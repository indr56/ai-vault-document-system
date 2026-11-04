"""FastAPI application: API under /api/v1, health probes, and the built web UI at /."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api.health import router as health_router
from app.api.v1 import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.security import ensure_demo_user
from app.db.session import get_async_engine, get_async_sessionmaker
from app.models import RequestMetric

logger = get_logger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
UNMETERED_PREFIXES = ("/health", "/assets", "/docs", "/openapi.json", "/redoc")
_background_tasks: set[asyncio.Task[None]] = set()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    async with get_async_sessionmaker()() as db:
        await ensure_demo_user(db)
    logger.info("api_started", environment=settings.environment, llm_provider=settings.llm_provider)
    yield
    await get_async_engine().dispose()


async def _store_request_metric(metric: RequestMetric) -> None:
    try:
        async with get_async_sessionmaker()() as db:
            db.add(metric)
            await db.commit()
    except Exception:
        logger.warning("request_metric_failed", exc_info=True)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=(
            "Upload documents, get AI insights, chat across them with page-level citations, compare them "
            "and monitor processing and AI cost.\n\nAuthenticate with the `X-API-Key` header "
            "(demo key: `vault-demo-key`)."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],  # Vite dev server
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000
            # Record the route *template* (e.g. /api/v1/documents/{document_id}) so metrics group per endpoint.
            template = getattr(request.scope.get("route"), "path", None)
            if template and request.url.path.startswith(settings.api_prefix) and settings.record_request_metrics:
                metric = RequestMetric(
                    method=request.method,
                    route=f"{settings.api_prefix}{template}",
                    status_code=status_code,
                    duration_ms=Decimal(str(round(duration_ms, 2))),
                    owner_id=getattr(request.state, "user_id", None),
                )
                task = asyncio.create_task(_store_request_metric(metric))
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
            if not request.url.path.startswith(UNMETERED_PREFIXES):
                logger.info(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=status_code,
                    duration_ms=round(duration_ms, 1),
                )

    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(api_router, prefix=settings.api_prefix)

    if STATIC_DIR.is_dir():  # the built React UI (copied in by the Docker build)
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


app = create_app()

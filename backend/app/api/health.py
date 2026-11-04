"""Liveness (`/health`) and readiness (`/health/ready`) probes, unauthenticated."""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.services.metrics import system_status

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness: the API process is up")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@router.get("/health/ready", summary="Readiness: database, Redis and a worker are available")
async def ready(db: AsyncSession = Depends(get_db)) -> JSONResponse:
    checks = await system_status(db)
    settings = get_settings()
    checks["ai_provider"] = {
        "ok": settings.llm_provider == "fake" or bool(settings.openai_api_key.get_secret_value()),
        "provider": settings.llm_provider,
    }
    ok = all(checks[k]["ok"] for k in ("database", "redis", "worker", "ai_provider"))
    return JSONResponse(status_code=200 if ok else 503, content={"status": "ready" if ok else "degraded", **checks})

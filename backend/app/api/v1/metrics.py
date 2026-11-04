from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rate_limit import rate_limited
from app.db.session import get_db
from app.models import User
from app.services import metrics as service

router = APIRouter(prefix="/metrics", tags=["metrics"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]
Days = Annotated[int, Query(ge=1, le=90, description="Look-back window in days")]


@router.get("/documents", summary="Document counts, processing success rate, stage timings, errors")
async def document_metrics(db: DB, user: CurrentUser, days: Days = 7) -> dict[str, Any]:
    return await service.document_metrics(db, user.id, days)


@router.get("/ai-usage", summary="Tokens, cost, latency by operation/model; chat quality rates")
async def ai_usage(db: DB, user: CurrentUser, days: Days = 7) -> dict[str, Any]:
    return await service.ai_usage_metrics(db, user.id, days)


@router.get("/api", summary="API performance per route (requests, p50/p95 latency, error rates)")
async def api_performance(db: DB, user: CurrentUser, hours: Annotated[int, Query(ge=1, le=168)] = 24) -> dict[str, Any]:
    return await service.api_metrics(db, hours)


@router.get("/system", summary="Database, Redis and worker health; queue depth")
async def system(db: DB, user: CurrentUser) -> dict[str, Any]:
    return await service.system_status(db)

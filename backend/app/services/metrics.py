"""Monitoring queries: document processing, AI usage & cost, chat quality and API performance.

All numbers come from data the system already records (processing runs, stage runs, AI usage
rows, chat messages, request metrics), so the dashboard has no separate source of truth.
"""

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import (
    AIUsage,
    ChatMessage,
    ChatSession,
    Document,
    DocumentInsight,
    ProcessingRun,
    RequestMetric,
    StageRun,
)
from app.models.enums import MessageRole, RunStatus
from app.services.cache import get_aredis

HEARTBEAT_KEY = "worker:heartbeat"
QUEUES = ("documents", "analysis")


def _since(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _p(column: Any, q: float) -> Any:
    return func.percentile_cont(q).within_group(column)


def _num(value: Any, digits: int = 1) -> float | None:
    return None if value is None else round(float(value), digits)


async def document_metrics(db: AsyncSession, owner_id: uuid.UUID, days: int) -> dict[str, Any]:
    by_status = dict(
        (
            await db.execute(
                select(Document.status, func.count())
                .where(Document.owner_id == owner_id, Document.is_latest.is_(True))
                .group_by(Document.status)
            )
        ).all()
    )
    totals = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(Document.size_bytes), 0),
                func.coalesce(func.sum(Document.page_count), 0),
                func.coalesce(func.sum(Document.chunk_count), 0),
            ).where(Document.owner_id == owner_id, Document.is_latest.is_(True))
        )
    ).one()
    by_category = dict(
        (
            await db.execute(
                select(DocumentInsight.category, func.count())
                .join(Document, Document.id == DocumentInsight.document_id)
                .where(Document.owner_id == owner_id, Document.is_latest.is_(True))
                .group_by(DocumentInsight.category)
            )
        ).all()
    )

    since = _since(days)
    runs = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(ProcessingRun.status == RunStatus.SUCCEEDED),
                func.count().filter(ProcessingRun.status == RunStatus.FAILED),
                func.avg(ProcessingRun.duration_ms).filter(ProcessingRun.status == RunStatus.SUCCEEDED),
                _p(ProcessingRun.duration_ms, 0.5).filter(ProcessingRun.status == RunStatus.SUCCEEDED),
                _p(ProcessingRun.duration_ms, 0.95).filter(ProcessingRun.status == RunStatus.SUCCEEDED),
                func.avg(ProcessingRun.queue_wait_ms),
            ).where(ProcessingRun.owner_id == owner_id, ProcessingRun.started_at >= since)
        )
    ).one()
    stages = (
        await db.execute(
            select(
                StageRun.stage,
                func.count(),
                func.avg(StageRun.duration_ms),
                _p(StageRun.duration_ms, 0.95),
                func.count().filter(StageRun.status == RunStatus.FAILED),
            )
            .join(ProcessingRun, ProcessingRun.id == StageRun.run_id)
            .where(ProcessingRun.owner_id == owner_id, ProcessingRun.started_at >= since)
            .group_by(StageRun.stage)
        )
    ).all()
    errors = (
        await db.execute(
            select(ProcessingRun.error_code, func.count())
            .where(
                ProcessingRun.owner_id == owner_id,
                ProcessingRun.started_at >= since,
                ProcessingRun.error_code.is_not(None),
            )
            .group_by(ProcessingRun.error_code)
            .order_by(func.count().desc())
        )
    ).all()
    total_runs, succeeded, failed = runs[0], runs[1], runs[2]
    return {
        "window_days": days,
        "documents": {
            "total": totals[0] or 0,
            "by_status": by_status,
            "by_category": by_category,
            "total_size_bytes": int(totals[1] or 0),
            "total_pages": int(totals[2] or 0),
            "total_chunks": int(totals[3] or 0),
        },
        "processing": {
            "runs": total_runs,
            "succeeded": succeeded,
            "failed": failed,
            "success_rate": _num(succeeded / total_runs, 3) if total_runs else None,
            "duration_ms": {"avg": _num(runs[3]), "p50": _num(runs[4]), "p95": _num(runs[5])},
            "avg_queue_wait_ms": _num(runs[6]),
            "stages": {
                stage: {"runs": n, "avg_ms": _num(avg), "p95_ms": _num(p95), "failed": f}
                for stage, n, avg, p95, f in stages
            },
            "errors": {code: n for code, n in errors},
        },
    }


async def ai_usage_metrics(db: AsyncSession, owner_id: uuid.UUID, days: int) -> dict[str, Any]:
    since = _since(days)
    scope = (AIUsage.owner_id == owner_id, AIUsage.created_at >= since)
    totals = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(func.sum(AIUsage.prompt_tokens), 0),
                func.coalesce(func.sum(AIUsage.completion_tokens), 0),
                func.coalesce(func.sum(AIUsage.cost_usd), 0),
                func.avg(AIUsage.latency_ms),
                func.count().filter(AIUsage.success.is_(False)),
            ).where(*scope)
        )
    ).one()
    by_operation = (
        await db.execute(
            select(
                AIUsage.operation,
                func.count(),
                func.sum(AIUsage.total_tokens),
                func.sum(AIUsage.cost_usd),
                func.avg(AIUsage.latency_ms),
                _p(AIUsage.latency_ms, 0.95),
            )
            .where(*scope)
            .group_by(AIUsage.operation)
            .order_by(func.sum(AIUsage.cost_usd).desc())
        )
    ).all()
    by_model = (
        await db.execute(
            select(AIUsage.model, func.count(), func.sum(AIUsage.total_tokens), func.sum(AIUsage.cost_usd))
            .where(*scope)
            .group_by(AIUsage.model)
        )
    ).all()
    day = func.date_trunc("day", AIUsage.created_at)
    daily = (
        await db.execute(
            select(day, func.sum(AIUsage.total_tokens), func.sum(AIUsage.cost_usd))
            .where(*scope)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    chat = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(ChatMessage.from_cache.is_(True)),
                func.count().filter(ChatMessage.abstained.is_(True)),
                func.count().filter(ChatMessage.grounded.is_(True)),
                func.avg(ChatMessage.latency_ms).filter(ChatMessage.from_cache.is_(False)),
                _p(ChatMessage.latency_ms, 0.95).filter(ChatMessage.from_cache.is_(False)),
            )
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.owner_id == owner_id,
                ChatMessage.role == MessageRole.ASSISTANT,
                ChatMessage.created_at >= since,
            )
        )
    ).one()
    answers = chat[0]
    return {
        "window_days": days,
        "totals": {
            "calls": totals[0],
            "prompt_tokens": int(totals[1]),
            "completion_tokens": int(totals[2]),
            "total_tokens": int(totals[1]) + int(totals[2]),
            "cost_usd": _num(totals[3], 6),
            "avg_latency_ms": _num(totals[4]),
            "failed_calls": totals[5],
        },
        "by_operation": [
            {
                "operation": op,
                "calls": n,
                "tokens": int(tokens or 0),
                "cost_usd": _num(cost, 6),
                "avg_latency_ms": _num(avg),
                "p95_latency_ms": _num(p95),
            }
            for op, n, tokens, cost, avg, p95 in by_operation
        ],
        "by_model": [
            {"model": m, "calls": n, "tokens": int(tokens or 0), "cost_usd": _num(cost, 6)}
            for m, n, tokens, cost in by_model
        ],
        "daily": [
            {"date": d.date().isoformat(), "tokens": int(tokens or 0), "cost_usd": _num(cost, 6)}
            for d, tokens, cost in daily
        ],
        "chat": {
            "answers": answers,
            "cache_hit_rate": _num(chat[1] / answers, 3) if answers else None,
            "abstention_rate": _num(chat[2] / answers, 3) if answers else None,
            "grounded_rate": _num(chat[3] / answers, 3) if answers else None,
            "avg_latency_ms": _num(chat[4]),
            "p95_latency_ms": _num(chat[5]),
        },
        "quota": {"daily_token_quota": get_settings().daily_token_quota},
    }


async def api_metrics(db: AsyncSession, hours: int) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(
                RequestMetric.method,
                RequestMetric.route,
                func.count(),
                func.avg(RequestMetric.duration_ms),
                _p(RequestMetric.duration_ms, 0.5),
                _p(RequestMetric.duration_ms, 0.95),
                func.sum(cast(RequestMetric.status_code >= 500, Integer)),
                func.sum(cast(RequestMetric.status_code.between(400, 499), Integer)),
            )
            .where(RequestMetric.created_at >= since)
            .group_by(RequestMetric.method, RequestMetric.route)
            .order_by(func.count().desc())
        )
    ).all()
    total = sum(r[2] for r in rows)
    server_errors = sum(int(r[6] or 0) for r in rows)
    return {
        "window_hours": hours,
        "requests": total,
        "server_error_rate": _num(server_errors / total, 4) if total else None,
        "routes": [
            {
                "method": method,
                "route": route,
                "requests": n,
                "avg_ms": _num(avg),
                "p50_ms": _num(p50),
                "p95_ms": _num(p95),
                "server_errors": int(e5 or 0),
                "client_errors": int(e4 or 0),
            }
            for method, route, n, avg, p50, p95, e5, e4 in rows
        ],
    }


async def system_status(db: AsyncSession) -> dict[str, Any]:
    """Component health plus queue depth: what an on-call engineer checks first."""
    result: dict[str, Any] = {}
    try:
        await db.execute(text("SELECT 1"))
        result["database"] = {"ok": True}
    except Exception as exc:
        result["database"] = {"ok": False, "error": type(exc).__name__}

    try:
        redis = get_aredis()
        await redis.ping()
        beat = await redis.get(HEARTBEAT_KEY)
        age = round(time.time() - float(beat), 1) if beat else None
        result["redis"] = {"ok": True}
        result["worker"] = {"ok": age is not None and age < 60, "last_heartbeat_seconds_ago": age}
    except Exception as exc:
        result["redis"] = {"ok": False, "error": type(exc).__name__}
        result["worker"] = {"ok": False, "last_heartbeat_seconds_ago": None}

    try:
        broker = aioredis.Redis.from_url(get_settings().celery_broker_url)
        try:
            result["queues"] = {q: await broker.llen(q) for q in QUEUES}
        finally:
            await broker.aclose()
    except Exception:
        result["queues"] = None
    return result

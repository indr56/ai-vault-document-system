"""Celery tasks: thin wrappers around services (business logic lives in app.services)."""

import time
import uuid

import structlog

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services import cache
from app.services.comparison import run_comparison_job
from app.services.processing import TransientProcessingError, run_pipeline
from app.services.processing import regenerate_insights as _regenerate
from app.workers.celery_app import celery_app

logger = get_logger(__name__)

HEARTBEAT_KEY = "worker:heartbeat"
RETRY_BACKOFF_SECONDS = (10, 30, 90)


@celery_app.task(
    bind=True, name="app.workers.tasks.process_document", max_retries=get_settings().processing_max_retries
)
def process_document(self, document_id: str) -> str:  # type: ignore[no-untyped-def]
    structlog.contextvars.bind_contextvars(document_id=document_id, task_id=self.request.id)
    is_final = self.request.retries >= self.max_retries
    try:
        result = run_pipeline(uuid.UUID(document_id), task_id=self.request.id, is_final_attempt=is_final)
    except TransientProcessingError as exc:
        countdown = RETRY_BACKOFF_SECONDS[min(self.request.retries, len(RETRY_BACKOFF_SECONDS) - 1)]
        logger.warning("document_processing_retry", retry_in_s=countdown, attempt=self.request.retries + 1)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        structlog.contextvars.clear_contextvars()
    return result.status


@celery_app.task(name="app.workers.tasks.regenerate_insights")
def regenerate_insights(document_id: str) -> str:
    return _regenerate(uuid.UUID(document_id))


@celery_app.task(name="app.workers.tasks.run_comparison", max_retries=2)
def run_comparison(comparison_id: str) -> str:
    return run_comparison_job(uuid.UUID(comparison_id))


@celery_app.task(name="app.workers.tasks.heartbeat")
def heartbeat() -> None:
    """Lets the API's readiness check know a worker is alive."""
    cache.get_redis().set(HEARTBEAT_KEY, str(time.time()), ex=60)


STALE_QUEUED_MINUTES = 10


@celery_app.task(name="app.workers.tasks.requeue_stale_documents")
def requeue_stale_documents() -> int:
    """Safety net: re-enqueue documents stuck in `queued` (e.g. the broker was down at upload time).
    Safe to run repeatedly because processing takes a per-document lock."""
    from datetime import timedelta

    from sqlalchemy import select

    from app.db.base import utcnow
    from app.db.session import sync_session
    from app.models import Document
    from app.models.enums import DocumentStatus

    cutoff = utcnow() - timedelta(minutes=STALE_QUEUED_MINUTES)
    with sync_session() as db:
        ids = (
            db.execute(
                select(Document.id)
                .where(Document.status == DocumentStatus.QUEUED, Document.updated_at < cutoff)
                .limit(100)
            )
            .scalars()
            .all()
        )
    for document_id in ids:
        process_document.delay(str(document_id))
    if ids:
        logger.info("requeued_stale_documents", count=len(ids))
    return len(ids)

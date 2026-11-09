"""Celery tasks: thin wrappers around services (business logic lives in app.services).

Every job task has a soft time limit (the job fails itself cleanly) and a hard one (the process is
killed), both below the 15-minute item locks. Transient provider errors retry with jittered backoff.
"""

import time
import uuid
from datetime import timedelta

import structlog
from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import sync_session
from app.models import Comparison, Document, ProcessingRun
from app.models.enums import ComparisonStatus, DocumentStatus, InsightsStatus, ProcessingStage, RunStatus
from app.services import cache, events
from app.services.comparison import run_comparison_job
from app.services.jobs import (
    TransientJobError,
    clear_enqueued,
    document_lock_key,
    is_locked,
    mark_enqueued,
    queue_backlog,
    retry_countdown,
    waiting_in_queue,
)
from app.services.processing import regenerate_insights as _regenerate
from app.services.processing import run_pipeline
from app.workers.celery_app import celery_app

logger = get_logger(__name__)
settings = get_settings()

HEARTBEAT_KEY = "worker:heartbeat"
TIME_LIMITS = {
    "soft_time_limit": settings.job_soft_time_limit_seconds,
    "time_limit": settings.job_hard_time_limit_seconds,
}


@celery_app.task(
    bind=True, name="app.workers.tasks.process_document", max_retries=settings.processing_max_retries, **TIME_LIMITS
)
def process_document(self, document_id: str) -> str:  # type: ignore[no-untyped-def]
    structlog.contextvars.bind_contextvars(document_id=document_id, task_id=self.request.id)
    is_final = self.request.retries >= self.max_retries
    try:
        result = run_pipeline(uuid.UUID(document_id), task_id=self.request.id, is_final_attempt=is_final)
    except TransientJobError as exc:
        countdown = retry_countdown(self.request.retries)
        logger.warning("document_processing_retry", retry_in_s=countdown, attempt=self.request.retries + 1)
        raise self.retry(exc=exc, countdown=countdown) from exc
    finally:
        structlog.contextvars.clear_contextvars()
    if result.status == "skipped_locked" and _still_queued(document_id):
        # Another job holds the lock (e.g. Reprocess clicked while insights were still being written):
        # this request still has to run, so try again shortly instead of dropping it.
        mark_enqueued("document", document_id)
        process_document.apply_async((document_id,), countdown=LOCKED_RETRY_SECONDS)
    return result.status


LOCKED_RETRY_SECONDS = 30


def _still_queued(document_id: str) -> bool:
    with sync_session() as db:
        return db.scalar(select(Document.status).where(Document.id == uuid.UUID(document_id))) == DocumentStatus.QUEUED


@celery_app.task(name="app.workers.tasks.regenerate_insights", **TIME_LIMITS)
def regenerate_insights(document_id: str) -> str:
    return _regenerate(uuid.UUID(document_id))


@celery_app.task(bind=True, name="app.workers.tasks.run_comparison", max_retries=2, **TIME_LIMITS)
def run_comparison(self, comparison_id: str) -> str:  # type: ignore[no-untyped-def]
    is_final = self.request.retries >= self.max_retries
    try:
        return run_comparison_job(uuid.UUID(comparison_id), is_final_attempt=is_final)
    except TransientJobError as exc:
        countdown = retry_countdown(self.request.retries)
        logger.warning("comparison_retry", comparison_id=comparison_id, retry_in_s=countdown)
        raise self.retry(exc=exc, countdown=countdown) from exc


@celery_app.task(name="app.workers.tasks.heartbeat")
def heartbeat() -> None:
    """Lets the API's readiness check know a worker is alive."""
    cache.get_redis().set(HEARTBEAT_KEY, str(time.time()), ex=60)


# ------------------------------------------------------------------------------------ safety net
STALE_QUEUED_MINUTES = 10  # queued/pending this long with no message waiting in the broker -> resend
# Running this long means the worker died (the hard time limit is 12 min): the job is not running.
STUCK_RUNNING_MINUTES = 20
REQUEUE_BATCH = 100


def _send(kind: str, task, item_id: uuid.UUID | str) -> None:  # type: ignore[no-untyped-def]
    # Marker first: a fast worker may pick the message up (and clear the marker) before .delay returns.
    mark_enqueued(kind, item_id)
    try:
        task.delay(str(item_id))
    except Exception:
        clear_enqueued(kind, item_id)
        raise


def enqueue_document(document_id: uuid.UUID | str) -> None:
    """Raises if the broker is unavailable (callers decide whether that is fatal)."""
    _send("document", process_document, document_id)


def enqueue_comparison(comparison_id: uuid.UUID | str) -> None:
    _send("comparison", run_comparison, comparison_id)


def enqueue_insights(document_id: uuid.UUID | str) -> None:
    _send("insights", regenerate_insights, document_id)


@celery_app.task(name="app.workers.tasks.requeue_stale_documents")
def requeue_stale_documents() -> int:
    """Runs every 5 minutes (beat). Finds work that was accepted but is not progressing:

    - documents `queued` / comparisons `pending` for 10+ minutes whose message is no longer waiting
      in the broker (e.g. the broker was down at upload time) -> send again;
    - documents `processing` / comparisons `running` for 20+ minutes (the worker died, since the hard
      time limit is 12 minutes) -> back to queued and resent while attempts remain, otherwise failed
      with `processing_timeout`;
    - insights `pending` for 20+ minutes on a ready document that no job is working on and no
      message is waiting for (the worker died mid-analysis) -> insights failed, so "Retry insights"
      appears instead of a spinner that never ends; processing runs left `running` are closed.
    A long backlog can outlive the 30-minute "enqueued" markers, so while a queue still has messages
    waiting, items without a marker get it back instead of a second message; once the queue has
    drained, anything still waiting really was lost and is resent.
    Safe to run repeatedly: every job takes a per-item lock and skips finished items.
    """
    now = utcnow()
    stale, stuck = now - timedelta(minutes=STALE_QUEUED_MINUTES), now - timedelta(minutes=STUCK_RUNNING_MINUTES)
    max_attempts = settings.processing_max_retries + 1
    resend_docs: list[uuid.UUID] = []
    resend_comparisons: list[uuid.UUID] = []
    with sync_session() as db:
        queued = list(
            db.execute(
                select(Document.id)
                .where(Document.status == DocumentStatus.QUEUED, Document.updated_at < stale)
                .limit(REQUEUE_BATCH)
            ).scalars()
        )
        resend_docs += _lost("document", queued)

        for doc in db.execute(
            select(Document)
            .where(Document.status == DocumentStatus.PROCESSING, Document.updated_at < stuck)
            .limit(REQUEUE_BATCH)
        ).scalars():
            if doc.attempts >= max_attempts:
                doc.status, doc.stage = DocumentStatus.FAILED, None
                doc.insights_status = InsightsStatus.FAILED
                doc.error_code = "processing_timeout"
                doc.error_message = "Processing was interrupted repeatedly and was stopped. You can retry it."
                events.publish(doc.owner_id, "document.failed", {"document_id": doc.id, "error_code": doc.error_code})
            else:
                doc.status, doc.stage = DocumentStatus.QUEUED, None
                resend_docs.append(doc.id)

        pending = list(
            db.execute(
                select(Comparison.id)
                .where(Comparison.status == ComparisonStatus.PENDING, Comparison.updated_at < stale)
                .limit(REQUEUE_BATCH)
            ).scalars()
        )
        resend_comparisons += _lost("comparison", pending)
        db.execute(
            update(Comparison)
            .where(Comparison.status == ComparisonStatus.RUNNING, Comparison.updated_at < stuck)
            .values(
                status=ComparisonStatus.FAILED,
                error_message="The comparison was interrupted. Please retry.",
                completed_at=now,
            )
        )

        _fail_abandoned_insights(db, stuck)
        db.execute(
            update(ProcessingRun)
            .where(ProcessingRun.status == RunStatus.RUNNING, ProcessingRun.started_at < stuck)
            .values(
                status=RunStatus.FAILED,
                finished_at=now,
                error_code="interrupted",
                error_message="The worker stopped before this run finished.",
            )
        )

    for document_id in resend_docs:
        enqueue_document(document_id)
    for comparison_id in resend_comparisons:
        enqueue_comparison(comparison_id)  # (if the broker is down this task itself isn't running)
    if resend_docs or resend_comparisons:
        logger.info("requeued_stale_jobs", documents=len(resend_docs), comparisons=len(resend_comparisons))
    return len(resend_docs) + len(resend_comparisons)


def _lost(kind: str, item_ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Items whose message is no longer in the broker (see requeue_stale_documents)."""
    unmarked = [i for i in item_ids if i not in waiting_in_queue(kind, item_ids)]
    if unmarked and queue_backlog(kind) > 0:
        for item_id in unmarked:  # probably still waiting behind a long backlog: don't send twice
            mark_enqueued(kind, item_id)
        return []
    return unmarked


def _fail_abandoned_insights(db, stuck) -> None:  # type: ignore[no-untyped-def]
    candidates = list(
        db.execute(
            select(Document)
            .where(
                Document.status == DocumentStatus.READY,
                Document.insights_status == InsightsStatus.PENDING,
                Document.updated_at < stuck,
            )
            .limit(REQUEUE_BATCH)
        ).scalars()
    )
    waiting = waiting_in_queue("insights", [d.id for d in candidates])
    for doc in candidates:
        if doc.id in waiting or is_locked(document_lock_key(doc.id)):
            continue  # still queued, or a job is working on it right now
        doc.insights_status = InsightsStatus.FAILED
        if doc.stage == ProcessingStage.ANALYZE:
            doc.stage = None
        logger.warning("insights_abandoned", document_id=str(doc.id))
        events.publish(doc.owner_id, "document.insights_failed", {"document_id": doc.id})

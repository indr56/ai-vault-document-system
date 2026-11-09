"""The asynchronous document pipeline (runs inside Celery workers).

    queued -> processing [parse -> chunk -> embed] -> ready -> [analyze -> insights]
                               \\-> failed (with error_code / error_message)

Design notes
- The document becomes `ready` (chat-able) as soon as embeddings are stored. Insights are a
  separate step: if they fail, chat still works and insights can be regenerated.
- Idempotent: a Redis lock prevents two workers processing the same document; chunks are replaced
  inside one transaction, so a retry never leaves duplicates.
- Bounded: the Celery soft time limit (below the lock TTL) fails a runaway job as
  "processing_timeout"; a database error inside a stage is rolled back before it is recorded.
- Every attempt is recorded as a ProcessingRun with per-stage timings (feeds the metrics API).
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import delete, insert
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import sync_session
from app.llm import get_provider
from app.llm.base import Usage
from app.models import Document, DocumentChunk, ProcessingRun, StageRun
from app.models.enums import DocumentStatus, InsightsStatus, ProcessingStage, RunStatus
from app.services import cache, events
from app.services.chunking import ChunkData, chunk_document
from app.services.insights import generate_insights
from app.services.jobs import TransientJobError, clear_enqueued, document_lock_key, redis_lock
from app.services.parsing import ParsedDocument, ParseError, parse_document
from app.services.storage import get_storage
from app.services.usage import failed_calls_to, record_entry, record_usage

logger = get_logger(__name__)

LOCK_TTL_SECONDS = 15 * 60  # above the job hard time limit (12 min), so a live job never loses it


class TransientProcessingError(TransientJobError):
    """Worth retrying (network / rate limit / provider outage)."""


@dataclass
class PipelineResult:
    status: str
    chunks: int = 0


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@contextmanager
def document_lock(document_id: uuid.UUID) -> Iterator[bool]:
    with redis_lock(document_lock_key(document_id), LOCK_TTL_SECONDS) as acquired:
        yield acquired


class StageRecorder:
    def __init__(self, db: Session, run: ProcessingRun, document: Document):
        self.db, self.run, self.document = db, run, document

    @contextmanager
    def stage(self, stage: ProcessingStage) -> Iterator[dict[str, Any]]:
        self.document.stage = stage
        self.db.commit()
        events.publish(self.document.owner_id, "document.stage", {"document_id": self.document.id, "stage": stage})
        details: dict[str, Any] = {}
        start = time.perf_counter()
        record = StageRun(run_id=self.run.id, stage=stage, status=RunStatus.RUNNING, started_at=utcnow())
        try:
            yield details
        except Exception as exc:
            # A failed SQL statement aborts the whole transaction; without a rollback the commit
            # below would fail too and hide the original error.
            self.db.rollback()
            record.status = RunStatus.FAILED
            record.error_message = str(exc)[:2000]
            raise
        else:
            record.status = RunStatus.SUCCEEDED
        finally:
            record.duration_ms = _ms(start)
            record.details = details
            self.db.add(record)
            self.db.commit()


def embed_chunks(db: Session, document: Document, chunks: list[ChunkData]) -> tuple[list[list[float]], int]:
    """Batched embedding with a Redis cache; returns vectors and how many were cache hits."""
    settings = get_settings()
    provider = get_provider()
    ids = {"owner_id": document.owner_id, "document_id": document.id}
    texts = [c.content for c in chunks]
    cached = cache.get_cached_embeddings(provider.embedding_model, texts)
    missing = [i for i, v in enumerate(cached) if v is None]
    vectors: list[list[float] | None] = list(cached)

    total_usage = Usage(model=provider.embedding_model)
    for start in range(0, len(missing), settings.embedding_batch_size):
        batch_idx = missing[start : start + settings.embedding_batch_size]
        with failed_calls_to(lambda e: record_entry(e, **ids), "embed_document", provider.embedding_model):
            result = provider.embed([texts[i] for i in batch_idx])
        for i, vector in zip(batch_idx, result.vectors, strict=True):
            vectors[i] = vector
        cache.set_cached_embeddings(provider.embedding_model, {texts[i]: vectors[i] for i in batch_idx})  # type: ignore[misc]
        total_usage.prompt_tokens += result.usage.prompt_tokens
        total_usage.latency_ms += result.usage.latency_ms
        total_usage.model = result.usage.model

    if missing:
        record_usage(total_usage, operation="embed_document", **ids)
    hits = len(chunks) - len(missing)
    if hits:  # what the cache saved: the same chunks embedded before (e.g. a reprocess)
        saved = Usage(
            provider.embedding_model,
            prompt_tokens=sum(c.token_count for c in chunks) - sum(chunks[i].token_count for i in missing),
        )
        record_usage(saved, operation="embed_document", cached=True, **ids)
    return [v for v in vectors if v is not None], hits


def _store_chunks(db: Session, document: Document, chunks: list[ChunkData], vectors: list[list[float]]) -> None:
    db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document.id))
    if chunks:
        db.execute(
            insert(DocumentChunk),
            [
                {
                    "id": uuid.uuid4(),
                    "document_id": document.id,
                    "owner_id": document.owner_id,
                    "chunk_index": c.index,
                    "page_number": c.page_number,
                    "content": c.content,
                    "token_count": c.token_count,
                    "embedding": v,
                }
                for c, v in zip(chunks, vectors, strict=True)
            ],
        )


def _fail(db: Session, document: Document, run: ProcessingRun, code: str, message: str) -> None:
    document.status = DocumentStatus.FAILED
    document.stage = None
    document.insights_status = InsightsStatus.FAILED  # nothing to analyse
    document.error_code = code
    document.error_message = message
    run.status = RunStatus.FAILED
    run.error_code = code
    run.error_message = message
    _finish_run(run)
    db.commit()
    events.publish(
        document.owner_id,
        "document.failed",
        {"document_id": document.id, "error_code": code, "error_message": message},
    )


def _finish_run(run: ProcessingRun) -> None:
    run.finished_at = utcnow()
    run.duration_ms = int((run.finished_at - run.started_at).total_seconds() * 1000)


def run_pipeline(document_id: uuid.UUID, *, task_id: str | None, is_final_attempt: bool) -> PipelineResult:
    clear_enqueued("document", document_id)  # picked up: no longer waiting in the broker
    with document_lock(document_id) as acquired:
        if not acquired:
            logger.info("document_already_processing", document_id=str(document_id))
            return PipelineResult(status="skipped_locked")
        with sync_session() as db:
            document = db.get(Document, document_id)
            if document is None:
                return PipelineResult(status="skipped_deleted")
            if document.status in (DocumentStatus.READY, DocumentStatus.FAILED):
                # Duplicate delivery (e.g. requeue safety net): already finished, nothing to do.
                return PipelineResult(status="skipped_done")
            return _process(db, document, task_id=task_id, is_final_attempt=is_final_attempt)


def _document_gone(db: Session, document_id: uuid.UUID) -> bool:
    return db.query(Document.id).filter(Document.id == document_id).first() is None


def _pages_without_text_warning(pages: list[int]) -> dict[str, str]:
    listed = ", ".join(str(p) for p in pages)
    if len(pages) == 1:
        subject, what, verb = f"Page {listed}", "a scanned image", "is"
    else:
        subject, what, verb = f"Pages {listed}", "scanned images", "are"
    return {
        "code": "pages_without_text",
        "message": f"{subject} had no extractable text (probably {what}) and {verb} not searchable; "
        "OCR is not supported yet.",
    }


def _process(db: Session, document: Document, *, task_id: str | None, is_final_attempt: bool) -> PipelineResult:
    settings = get_settings()
    document_id = document.id  # kept as a plain value: usable even if the row is deleted mid-job
    document.status = DocumentStatus.PROCESSING
    document.attempts += 1
    document.error_code = None
    document.error_message = None
    run = ProcessingRun(
        document_id=document.id,
        owner_id=document.owner_id,
        attempt=document.attempts,
        task_id=task_id,
        started_at=utcnow(),
        queue_wait_ms=int((utcnow() - document.updated_at).total_seconds() * 1000),
    )
    db.add(run)
    db.commit()
    events.publish(document.owner_id, "document.processing", {"document_id": document.id})
    recorder = StageRecorder(db, run, document)
    pipeline_start = time.perf_counter()

    try:
        with recorder.stage(ProcessingStage.PARSE) as details:
            parsed: ParsedDocument = parse_document(get_storage().path(document.storage_path), document.extension)
            details["pages"] = len(parsed.pages)
            document.page_count = parsed.page_count
            document.warnings = []
            if parsed.pages_without_text:
                details["pages_without_text"] = parsed.pages_without_text
                document.warnings = [_pages_without_text_warning(parsed.pages_without_text)]

        with recorder.stage(ProcessingStage.CHUNK) as details:
            chunks = chunk_document(parsed, settings.chunk_size_tokens, settings.chunk_overlap_tokens)
            details["chunks"] = len(chunks)
            if not chunks:
                raise ParseError("empty_document", "The document contains no readable text.")

        with recorder.stage(ProcessingStage.EMBED) as details:
            vectors, cache_hits = embed_chunks(db, document, chunks)
            details["cache_hits"] = cache_hits
            _store_chunks(db, document, chunks, vectors)

    except ParseError as exc:
        _fail(db, document, run, exc.code, exc.message)
        return PipelineResult(status="failed")
    except SoftTimeLimitExceeded:
        logger.error("document_processing_timeout", document_id=str(document_id))
        db.rollback()
        _fail(
            db,
            document,
            run,
            "processing_timeout",
            "Processing took too long and was stopped. The file may be too large or unusual; you can retry it.",
        )
        return PipelineResult(status="failed")
    except Exception as exc:
        from app.llm.openai_provider import TRANSIENT_ERRORS

        db.rollback()
        if _document_gone(db, document_id):
            logger.info("document_deleted_during_processing", document_id=str(document_id))
            return PipelineResult(status="skipped_deleted")
        transient = isinstance(exc, TRANSIENT_ERRORS)
        logger.exception("document_processing_error", document_id=str(document_id), transient=transient)
        if transient and not is_final_attempt:
            run.status = RunStatus.FAILED
            run.error_code = "transient_error"
            run.error_message = str(exc)[:2000]
            _finish_run(run)
            document.status = DocumentStatus.QUEUED  # will be retried
            document.stage = None
            db.commit()
            raise TransientProcessingError(str(exc)) from exc
        _fail(db, document, run, "processing_error", "Processing failed unexpectedly. You can retry it.")
        return PipelineResult(status="failed")

    # Chat-ready. From here the row can legitimately disappear: delete is allowed once a document is
    # READY, while this worker is still writing insights. That ends the job quietly (skipped_deleted).
    owner_id = document.owner_id
    try:
        document.status = DocumentStatus.READY
        document.stage = ProcessingStage.ANALYZE
        document.chunk_count = len(chunks)
        document.token_count = sum(c.token_count for c in chunks)
        document.processed_at = utcnow()
        db.commit()
        events.publish(owner_id, "document.ready", {"document_id": document_id, "chunks": len(chunks)})

        if not analyze_document(db, document, parsed, recorder):
            return PipelineResult(status="skipped_deleted")

        document.stage = None
        document.processing_ms = _ms(pipeline_start)
        run.status = RunStatus.SUCCEEDED
        _finish_run(run)
        db.commit()
    except (StaleDataError, ObjectDeletedError):
        db.rollback()
        if _document_gone(db, document_id):
            logger.info("document_deleted_during_processing", document_id=str(document_id))
            return PipelineResult(status="skipped_deleted")
        raise
    return PipelineResult(status="ready", chunks=len(chunks))


def analyze_document(db: Session, document: Document, parsed: ParsedDocument, recorder: StageRecorder) -> bool:
    """Insights step. Failures are isolated: the document stays chat-ready.

    Returns False if the document was deleted meanwhile (nothing left to write insights for)."""
    document_id, owner_id = document.id, document.owner_id  # plain values: usable after a rollback
    try:
        with recorder.stage(ProcessingStage.ANALYZE) as details:
            insight = generate_insights(db, document, parsed)
            details["category"] = insight.category
            document.insights_status = InsightsStatus.READY
        events.publish(owner_id, "document.insights_ready", {"document_id": document_id})
    except Exception:
        db.rollback()
        if _document_gone(db, document_id):
            logger.info("document_deleted_during_insights", document_id=str(document_id))
            return False
        logger.exception("insights_failed", document_id=str(document_id))
        document.insights_status = InsightsStatus.FAILED
        events.publish(owner_id, "document.insights_failed", {"document_id": document_id})
    db.commit()
    return True


def regenerate_insights(document_id: uuid.UUID) -> str:
    """Re-run only the analysis step (e.g. after an insights failure or a prompt change).

    Always ends with insights_status ready or failed, never stuck at "pending": any error reading
    the file again (missing, corrupt, a parser bug) or a timeout marks the insights failed; the
    document itself stays chat-ready. A worker killed outright is handled by the requeue sweep.
    """
    clear_enqueued("insights", document_id)  # picked up: no longer waiting in the broker
    with document_lock(document_id) as acquired:
        if not acquired:
            return "skipped_locked"  # the document is being (re)processed, which produces insights anyway
        with sync_session() as db:
            document = db.get(Document, document_id)
            if document is None or document.status != DocumentStatus.READY:
                return "skipped"
            run = ProcessingRun(
                document_id=document.id, owner_id=document.owner_id, attempt=document.attempts, started_at=utcnow()
            )
            db.add(run)
            db.commit()
            try:
                parsed = parse_document(get_storage().path(document.storage_path), document.extension)
            except Exception as exc:  # incl. SoftTimeLimitExceeded; a parser bug must not leave "pending"
                logger.exception("insights_regeneration_failed", document_id=str(document.id))
                document.insights_status = InsightsStatus.FAILED
                run.status = RunStatus.FAILED
                run.error_code = exc.code if isinstance(exc, ParseError) else "processing_error"
                run.error_message = str(exc)[:2000]
                _finish_run(run)
                db.commit()
                events.publish(document.owner_id, "document.insights_failed", {"document_id": document.id})
                return str(document.insights_status)
            if not analyze_document(db, document, parsed, StageRecorder(db, run, document)):
                return "skipped_deleted"
            run.status = RunStatus.SUCCEEDED if document.insights_status == InsightsStatus.READY else RunStatus.FAILED
            _finish_run(run)
            document.stage = None
            db.commit()
            return str(document.insights_status)

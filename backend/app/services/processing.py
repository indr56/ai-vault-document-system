"""The asynchronous document pipeline (runs inside Celery workers).

    queued -> processing [parse -> chunk -> embed] -> ready -> [analyze -> insights]
                               \\-> failed (with error_code / error_message)

Design notes
- The document becomes `ready` (chat-able) as soon as embeddings are stored. Insights are a
  separate step: if they fail, chat still works and insights can be regenerated.
- Idempotent: a Redis lock prevents two workers processing the same document; chunks are replaced
  inside one transaction, so a retry never leaves duplicates.
- Every attempt is recorded as a ProcessingRun with per-stage timings (feeds the metrics API).
"""

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

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
from app.services.parsing import ParsedDocument, ParseError, parse_document
from app.services.storage import get_storage
from app.services.usage import record_usage

logger = get_logger(__name__)

LOCK_TTL_SECONDS = 15 * 60


class TransientProcessingError(Exception):
    """Worth retrying (network / rate limit / provider outage)."""


@dataclass
class PipelineResult:
    status: str
    chunks: int = 0


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


@contextmanager
def document_lock(document_id: uuid.UUID) -> Iterator[bool]:
    key = f"lock:document:{document_id}"
    acquired = bool(cache.get_redis().set(key, "1", nx=True, ex=LOCK_TTL_SECONDS))
    try:
        yield acquired
    finally:
        if acquired:
            cache.get_redis().delete(key)


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
    texts = [c.content for c in chunks]
    cached = cache.get_cached_embeddings(provider.embedding_model, texts)
    missing = [i for i, v in enumerate(cached) if v is None]
    vectors: list[list[float] | None] = list(cached)

    total_usage = Usage(model=provider.embedding_model)
    for start in range(0, len(missing), settings.embedding_batch_size):
        batch_idx = missing[start : start + settings.embedding_batch_size]
        result = provider.embed([texts[i] for i in batch_idx])
        for i, vector in zip(batch_idx, result.vectors, strict=True):
            vectors[i] = vector
        cache.set_cached_embeddings(provider.embedding_model, {texts[i]: vectors[i] for i in batch_idx})  # type: ignore[misc]
        total_usage.prompt_tokens += result.usage.prompt_tokens
        total_usage.latency_ms += result.usage.latency_ms
        total_usage.model = result.usage.model

    if missing:
        record_usage(db, total_usage, operation="embed_document", owner_id=document.owner_id, document_id=document.id)
    return [v for v in vectors if v is not None], len(chunks) - len(missing)


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


def _process(db: Session, document: Document, *, task_id: str | None, is_final_attempt: bool) -> PipelineResult:
    settings = get_settings()
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
    except Exception as exc:
        from app.llm.openai_provider import TRANSIENT_ERRORS

        transient = isinstance(exc, TRANSIENT_ERRORS)
        logger.exception("document_processing_error", document_id=str(document.id), transient=transient)
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

    # Chat-ready.
    document.status = DocumentStatus.READY
    document.stage = ProcessingStage.ANALYZE
    document.chunk_count = len(chunks)
    document.token_count = sum(c.token_count for c in chunks)
    document.processed_at = utcnow()
    db.commit()
    events.publish(document.owner_id, "document.ready", {"document_id": document.id, "chunks": len(chunks)})

    analyze_document(db, document, parsed, recorder)

    document.stage = None
    document.processing_ms = _ms(pipeline_start)
    run.status = RunStatus.SUCCEEDED
    _finish_run(run)
    db.commit()
    return PipelineResult(status="ready", chunks=len(chunks))


def analyze_document(db: Session, document: Document, parsed: ParsedDocument, recorder: StageRecorder) -> None:
    """Insights step. Failures are isolated: the document stays chat-ready."""
    try:
        with recorder.stage(ProcessingStage.ANALYZE) as details:
            insight = generate_insights(db, document, parsed)
            details["category"] = insight.category
            document.insights_status = InsightsStatus.READY
        events.publish(document.owner_id, "document.insights_ready", {"document_id": document.id})
    except Exception:
        logger.exception("insights_failed", document_id=str(document.id))
        db.rollback()
        document.insights_status = InsightsStatus.FAILED
        events.publish(document.owner_id, "document.insights_failed", {"document_id": document.id})
    db.commit()


def regenerate_insights(document_id: uuid.UUID) -> str:
    """Re-run only the analysis step (e.g. after an insights failure or a prompt change)."""
    with sync_session() as db:
        document = db.get(Document, document_id)
        if document is None or document.status != DocumentStatus.READY:
            return "skipped"
        parsed = parse_document(get_storage().path(document.storage_path), document.extension)
        run = ProcessingRun(
            document_id=document.id, owner_id=document.owner_id, attempt=document.attempts, started_at=utcnow()
        )
        db.add(run)
        db.commit()
        analyze_document(db, document, parsed, StageRecorder(db, run, document))
        run.status = RunStatus.SUCCEEDED if document.insights_status == InsightsStatus.READY else RunStatus.FAILED
        _finish_run(run)
        document.stage = None
        db.commit()
        return str(document.insights_status)

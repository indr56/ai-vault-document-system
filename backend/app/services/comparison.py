"""Comparative analysis between documents (async job).

For each document we assemble a compact evidence pack: its AI summary + key facts (with pages),
plus the chunks most relevant to the comparison focus (hybrid retrieval within that document).
The model then returns a structured, page-referenced comparison.
"""

import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import sync_session
from app.llm import get_provider
from app.llm.base import Usage
from app.llm.prompts import comparison as comparison_prompt
from app.llm.schemas import ComparisonOutput
from app.models import Comparison, Document, DocumentInsight
from app.models.enums import ComparisonStatus
from app.services import events
from app.services.cache import get_cached_embeddings, set_cached_embeddings
from app.services.chunking import count_tokens
from app.services.jobs import TransientJobError, clear_enqueued, redis_lock
from app.services.retrieval import hybrid_search_sync
from app.services.usage import failed_calls_to, record_entry, record_usage

logger = get_logger(__name__)

# Used when the user gives no focus: generic and document-type neutral. The keyword side of hybrid
# search matches chunks containing ANY of these words (see retrieval._any_words_tsquery).
DEFAULT_FOCUS_QUERY = "key terms, amounts, prices, limits, dates, deadlines, parties, obligations, conditions"
CHUNKS_PER_DOCUMENT = 8
LABELS = "ABCDEFGHIJ"


def _embed_focus(comparison: Comparison) -> tuple[str, list[float]]:
    """Embed the focus once per comparison (cached across comparisons) and record its cost."""
    provider = get_provider()
    query = comparison.focus or DEFAULT_FOCUS_QUERY
    cached = get_cached_embeddings(provider.embedding_model, [query])[0]
    if cached is not None:
        saved = Usage(provider.embedding_model, prompt_tokens=count_tokens(query))
        record_usage(saved, operation="embed_query", owner_id=comparison.owner_id, cached=True)
        return query, cached
    with failed_calls_to(
        lambda e: record_entry(e, owner_id=comparison.owner_id), "embed_query", provider.embedding_model
    ):
        result = provider.embed([query])
    set_cached_embeddings(provider.embedding_model, {query: result.vectors[0]})
    record_usage(result.usage, operation="embed_query", owner_id=comparison.owner_id)
    return query, result.vectors[0]


def _evidence_pack(db: Session, label: str, document: Document, query: str, embedding: list[float]) -> str:
    chunks = hybrid_search_sync(
        db,
        owner_id=document.owner_id,
        document_ids=[document.id],
        query=query,
        embedding=embedding,
        top_k=CHUNKS_PER_DOCUMENT,
    )
    chunks.sort(key=lambda c: c.chunk_index)
    insight = db.get(DocumentInsight, document.id)
    parts = [f"=== Document {label}: {document.title} (version {document.version}) ==="]
    if insight:
        parts.append(f"Summary: {insight.summary}")
        if insight.key_facts:
            facts = "; ".join(f"{f['label']}: {f['value']} (p. {f.get('page')})" for f in insight.key_facts)
            parts.append(f"Key facts: {facts}")
    parts.append("Relevant excerpts:")
    for c in chunks:
        page = f"page {c.page_number}" if c.page_number else "no page"
        parts.append(f"[{label}, {page}] {c.content}")
    return "\n".join(parts)


COMPARISON_LOCK_TTL_SECONDS = 15 * 60


def run_comparison_job(comparison_id: uuid.UUID, *, is_final_attempt: bool = True) -> str:
    """Runs one comparison. Always ends completed or failed, except for a transient provider error
    with retries left: then the row goes back to pending and TransientJobError asks Celery to retry."""
    from app.llm.openai_provider import TRANSIENT_ERRORS

    clear_enqueued("comparison", comparison_id)
    with redis_lock(f"lock:comparison:{comparison_id}", COMPARISON_LOCK_TTL_SECONDS) as acquired:
        if not acquired:
            return "skipped_locked"
        with sync_session() as db:
            comparison = db.get(Comparison, comparison_id)
            if comparison is None or comparison.status in (ComparisonStatus.COMPLETED, ComparisonStatus.FAILED):
                return "skipped"
            comparison.status = ComparisonStatus.RUNNING
            db.commit()
            try:
                _compare(db, comparison)
                comparison.status = ComparisonStatus.COMPLETED
            except Exception as exc:
                db.rollback()
                if isinstance(exc, TRANSIENT_ERRORS) and not is_final_attempt:
                    logger.warning("comparison_transient_error", comparison_id=str(comparison_id), error=str(exc))
                    comparison.status = ComparisonStatus.PENDING
                    db.commit()
                    raise TransientJobError(str(exc)) from exc
                logger.exception("comparison_failed", comparison_id=str(comparison_id))
                comparison.status = ComparisonStatus.FAILED
                if isinstance(exc, ValueError):
                    comparison.error_message = str(exc)[:1000]
                elif isinstance(exc, SoftTimeLimitExceeded):
                    comparison.error_message = "The comparison took too long and was stopped. Please retry."
                else:
                    comparison.error_message = "The comparison failed. Please retry."
            comparison.completed_at = utcnow()
            db.commit()
            events.publish(comparison.owner_id, f"comparison.{comparison.status}", {"comparison_id": comparison.id})
            return str(comparison.status)


def _compare(db: Session, comparison: Comparison) -> None:
    documents = [db.get(Document, uuid.UUID(d)) for d in comparison.document_ids]
    if any(d is None for d in documents):
        raise ValueError("One of the documents no longer exists.")
    query, embedding = _embed_focus(comparison)
    material = "\n\n".join(
        _evidence_pack(db, LABELS[i], doc, query, embedding)  # type: ignore[arg-type]
        for i, doc in enumerate(documents)
    )
    provider = get_provider()
    sink = lambda e: record_entry(e, owner_id=comparison.owner_id)  # noqa: E731
    with failed_calls_to(sink, "comparison", provider.chat_model, comparison_prompt.VERSION):
        result = provider.complete_structured(
            comparison_prompt.build_messages(material, comparison.focus), ComparisonOutput, max_tokens=3000
        )
    record_usage(
        result.usage,
        operation="comparison",
        owner_id=comparison.owner_id,
        prompt_version=comparison_prompt.VERSION,
    )
    label_map = {LABELS[i]: {"document_id": str(d.id), "title": d.title} for i, d in enumerate(documents)}  # type: ignore[union-attr]
    comparison.result = {"labels": label_map, **result.value.model_dump()}
    comparison.model = result.usage.model

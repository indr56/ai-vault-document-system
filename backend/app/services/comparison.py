"""Comparative analysis between documents (async job).

For each document we assemble a compact evidence pack: its AI summary + key facts (with pages),
plus the chunks most relevant to the comparison focus (hybrid retrieval within that document).
The model then returns a structured, page-referenced comparison.
"""

import uuid

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import sync_session
from app.llm import get_provider
from app.llm.prompts import comparison as comparison_prompt
from app.llm.schemas import ComparisonOutput
from app.models import Comparison, Document, DocumentInsight
from app.models.enums import ComparisonStatus
from app.services import events
from app.services.cache import get_cached_embeddings, set_cached_embeddings
from app.services.retrieval import hybrid_search_sync
from app.services.usage import record_usage

logger = get_logger(__name__)

# Used when the user gives no focus: generic and document-type neutral. "or" makes the keyword
# side of hybrid search match any of the terms (websearch syntax), not all of them.
DEFAULT_FOCUS_QUERY = (
    "key terms or amounts or prices or limits or dates or deadlines or parties or obligations or conditions"
)
CHUNKS_PER_DOCUMENT = 8
LABELS = "ABCDEFGHIJ"


def _embed_focus(db: Session, comparison: Comparison) -> tuple[str, list[float]]:
    """Embed the focus once per comparison (cached across comparisons) and record its cost."""
    provider = get_provider()
    query = comparison.focus or DEFAULT_FOCUS_QUERY
    cached = get_cached_embeddings(provider.embedding_model, [query])[0]
    if cached is not None:
        return query, cached
    result = provider.embed([query])
    set_cached_embeddings(provider.embedding_model, {query: result.vectors[0]})
    record_usage(db, result.usage, operation="embed_query", owner_id=comparison.owner_id)
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


def run_comparison_job(comparison_id: uuid.UUID) -> str:
    with sync_session() as db:
        comparison = db.get(Comparison, comparison_id)
        if comparison is None:
            return "skipped"
        comparison.status = ComparisonStatus.RUNNING
        db.commit()
        try:
            documents = [db.get(Document, uuid.UUID(d)) for d in comparison.document_ids]
            if any(d is None for d in documents):
                raise ValueError("One of the documents no longer exists.")
            query, embedding = _embed_focus(db, comparison)
            material = "\n\n".join(
                _evidence_pack(db, LABELS[i], doc, query, embedding)  # type: ignore[arg-type]
                for i, doc in enumerate(documents)
            )
            provider = get_provider()
            result = provider.complete_structured(
                comparison_prompt.build_messages(material, comparison.focus), ComparisonOutput, max_tokens=3000
            )
            record_usage(
                db,
                result.usage,
                operation="comparison",
                owner_id=comparison.owner_id,
                prompt_version=comparison_prompt.VERSION,
            )
            label_map = {LABELS[i]: {"document_id": str(d.id), "title": d.title} for i, d in enumerate(documents)}  # type: ignore[union-attr]
            comparison.result = {"labels": label_map, **result.value.model_dump()}
            comparison.model = result.usage.model
            comparison.status = ComparisonStatus.COMPLETED
        except Exception as exc:
            logger.exception("comparison_failed", comparison_id=str(comparison_id))
            db.rollback()
            comparison.status = ComparisonStatus.FAILED
            comparison.error_message = (
                str(exc)[:1000] if isinstance(exc, ValueError) else "The comparison failed. Please retry."
            )
        comparison.completed_at = utcnow()
        db.commit()
        events.publish(comparison.owner_id, f"comparison.{comparison.status}", {"comparison_id": comparison.id})
        return str(comparison.status)

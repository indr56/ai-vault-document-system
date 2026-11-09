"""Comparative analysis between documents (async job).

For each document we assemble a compact evidence pack: its AI summary + key facts (with pages),
plus the chunks most relevant to the comparison focus (hybrid retrieval within that document).
When the documents are versions of one document, the material also lists the exact changes
between their texts (a word-level diff), so the model can't overlook one. The model then returns a
structured, page-referenced comparison.
"""

import difflib
import re
import uuid

from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.base import utcnow
from app.db.session import sync_session
from app.llm import get_provider
from app.llm.base import Usage
from app.llm.prompts import comparison as comparison_prompt
from app.llm.schemas import ComparisonOutput
from app.models import Comparison, Document, DocumentChunk, DocumentInsight
from app.models.enums import ComparisonStatus
from app.services import events
from app.services.cache import get_cached_embeddings, set_cached_embeddings
from app.services.chunking import count_tokens
from app.services.jobs import TransientJobError, clear_enqueued, comparison_lock_key, redis_lock
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


# Versions of one document: list the exact text changes. Measured on the sample policy's renewal
# (9 Nov, 5 runs each): without the list the model missed the added cyber exclusion in 4 of 5 runs;
# with it, every change was reported in 5 of 5. Asking for it in the instructions instead backfired:
# comparisons of DIFFERENT documents then listed every exclusion separately and ran out of space.
MAX_DIFF_WORDS = 30_000  # per document; longer ones skip the list (the diff is O(n*m) at worst)
MAX_CHANGES = 40
CHANGE_CONTEXT_WORDS = 5
MAX_CHANGE_CHARS = 400


def _full_text(db: Session, document: Document) -> str:
    rows = db.execute(
        select(DocumentChunk.content)
        .where(DocumentChunk.document_id == document.id)
        .order_by(DocumentChunk.chunk_index)
    ).scalars()
    return " ".join(rows)


def version_changes(new_text: str, old_text: str, new_label: str, old_label: str) -> list[str]:
    """Word-level changes from old to new, each with a few words of context:
    '...Annual premium [B: "$42,300" -> A: "$48,750"]', '...design. [only in A: "(g) Cyber incident..."]'."""
    new_words, old_words = new_text.split(), old_text.split()
    if max(len(new_words), len(old_words)) > MAX_DIFF_WORDS:
        return []
    changes = []
    matcher = difflib.SequenceMatcher(a=old_words, b=new_words, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        before = " ".join(old_words[max(0, i1 - CHANGE_CONTEXT_WORDS) : i1])
        old, new = " ".join(old_words[i1:i2]), " ".join(new_words[j1:j2])
        if tag == "replace":
            change = f'[{old_label}: "{old}" -> {new_label}: "{new}"]'
        elif tag == "insert":
            change = f'[only in {new_label}: "{new}"]'
        else:
            change = f'[only in {old_label}: "{old}"]'
        changes.append(f"...{before} {change}"[:MAX_CHANGE_CHARS])
    return changes[:MAX_CHANGES]


def _version_changes_section(db: Session, documents: list[Document]) -> str:
    """For versions of one document only; empty for different documents (their comparison is unchanged)."""
    if len({d.version_group_id for d in documents}) != 1:
        return ""
    texts = [_full_text(db, d) for d in documents]
    lines = []
    for i in range(1, len(documents)):
        lines += version_changes(texts[0], texts[i], LABELS[0], LABELS[i])
    lines = list(dict.fromkeys(lines))  # chunks overlap, so a change can appear twice
    if not lines:
        return ""
    return (
        "=== Changes between versions (word-level comparison of the texts; every change below must be "
        "covered in differences) ===\n" + "\n".join(lines)
    )


_NOT_ALPHANUMERIC = re.compile(r"[^a-z0-9%]+")


def _differs(difference: dict) -> bool:  # type: ignore[type-arg]
    """False when every document has the same value (the model sometimes lists "No change" rows)."""
    values = {_NOT_ALPHANUMERIC.sub("", str(v["value"]).lower()) for v in difference["values"]}
    return len(values) > 1


COMPARISON_LOCK_TTL_SECONDS = 15 * 60


def run_comparison_job(comparison_id: uuid.UUID, *, is_final_attempt: bool = True) -> str:
    """Runs one comparison. Always ends completed or failed, except for a transient provider error
    with retries left: then the row goes back to pending and TransientJobError asks Celery to retry."""
    from app.llm.openai_provider import TRANSIENT_ERRORS

    clear_enqueued("comparison", comparison_id)
    with redis_lock(comparison_lock_key(comparison_id), COMPARISON_LOCK_TTL_SECONDS) as acquired:
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
    changes = _version_changes_section(db, documents)  # type: ignore[arg-type]
    if changes:
        material += "\n\n" + changes
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
    label_map = {
        LABELS[i]: {"document_id": str(d.id), "title": d.title, "version": d.version}  # type: ignore[union-attr]
        for i, d in enumerate(documents)
    }
    output = result.value.model_dump()
    output["differences"] = [d for d in output["differences"] if _differs(d)]  # same values: not a difference
    comparison.result = {"labels": label_map, **output}
    comparison.model = result.usage.model

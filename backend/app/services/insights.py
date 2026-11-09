"""AI document understanding (runs in the worker): summary, category, tags, key facts with pages,
entities, sentiment and suggested questions. Long documents use map-reduce."""

import hashlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm import get_provider
from app.llm.base import Usage
from app.llm.prompts import insights as insights_prompt
from app.llm.schemas import InsightsOutput, SectionNotes
from app.models import Document, DocumentInsight
from app.services import cache
from app.services.chunking import count_tokens, split_by_tokens
from app.services.parsing import ParsedDocument
from app.services.usage import UsageEntry, failed_calls_to, record_entry, record_usage

logger = get_logger(__name__)

SECTION_TOKENS = 12_000  # one map call reads at most this much
MAX_REDUCE_LEVELS = 3  # 3 levels of 12k-token groups cover far more than any realistic document
NOTES_CACHE_TTL_SECONDS = 7 * 24 * 3600


def _group(blocks: list[str], max_tokens: int) -> list[str]:
    """Pack consecutive blocks into groups of at most max_tokens; a block larger than that is split."""
    groups: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        for piece in [block] if count_tokens(block) <= max_tokens else split_by_tokens(block, max_tokens):
            tokens = count_tokens(piece)
            if current and size + tokens > max_tokens:
                groups.append("\n\n".join(current))
                current, size = [], 0
            current.append(piece)
            size += tokens
    if current:
        groups.append("\n\n".join(current))
    return groups


def _split_sections(parsed: ParsedDocument, max_tokens: int) -> list[str]:
    return _group([f"[Page {p.number}]\n{p.text}" if p.number else p.text for p in parsed.pages], max_tokens)


def _failure_sink(document: Document) -> Callable[[UsageEntry], None]:
    return lambda entry: record_entry(entry, owner_id=document.owner_id, document_id=document.id)


def _notes_cache_key(model: str, section: str) -> str:
    digest = hashlib.sha256(section.encode()).hexdigest()
    return f"notes:{model}:{insights_prompt.VERSION}:{digest}"


def _section_notes(document: Document, section: str) -> str:
    """One map call, cached by the section's content: regenerating insights or writing a custom
    summary of the same long document reuses the notes instead of paying for the map step again."""
    provider = get_provider()
    ids = {"owner_id": document.owner_id, "document_id": document.id}
    key = _notes_cache_key(provider.fast_model, section)
    hit = cache.get_json(key)
    if hit is not None:
        record_usage(
            Usage(**hit["usage"]),
            operation="insights_section_notes",
            cached=True,
            **ids,
            prompt_version=insights_prompt.VERSION,
        )
        return str(hit["notes"])
    messages = insights_prompt.build_section_notes_messages(document.title, section)
    with failed_calls_to(
        _failure_sink(document), "insights_section_notes", provider.fast_model, insights_prompt.VERSION
    ):
        result = provider.complete_structured(messages, SectionNotes, model=provider.fast_model, max_tokens=1500)
    record_usage(result.usage, operation="insights_section_notes", prompt_version=insights_prompt.VERSION, **ids)
    cache.set_json(key, {"notes": result.value.notes, "usage": result.usage.__dict__}, NOTES_CACHE_TTL_SECONDS)
    return result.value.notes


def _notes_for(document: Document, sections: list[str]) -> list[str]:
    """Map calls run in parallel (they are independent), in the original order. ~1,000 pages are
    ~80 sections: one after another at ~10 s each that is over 13 minutes, past the job's time
    limit; 8 at a time it is under 2 minutes."""
    if len(sections) == 1:
        return [_section_notes(document, sections[0])]
    pool = ThreadPoolExecutor(max_workers=min(get_settings().insights_map_concurrency, len(sections)))
    try:
        return list(pool.map(lambda section: _section_notes(document, section), sections))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # on a timeout, don't start the remaining calls


def needs_condensing(parsed: ParsedDocument) -> bool:
    return count_tokens(parsed.text_with_page_markers) > get_settings().insights_max_input_tokens


def prepare_analysis_text(document: Document, parsed: ParsedDocument) -> str:
    """Full text when it fits the budget; otherwise map-reduce:

    level 1: notes for each ~12k-token section;
    level 2+: if the joined notes still exceed the budget (very long documents: ~1,000 pages give
    ~80 sections of notes), the notes are grouped and condensed again, until they fit.
    """
    budget = get_settings().insights_max_input_tokens
    text = parsed.text_with_page_markers
    if count_tokens(text) <= budget:
        return text

    notes = _notes_for(document, _split_sections(parsed, SECTION_TOKENS))
    levels = 1
    while count_tokens("\n\n".join(notes)) > budget and levels < MAX_REDUCE_LEVELS:
        notes = _notes_for(document, _group(notes, SECTION_TOKENS))
        levels += 1
    joined = "\n\n".join(notes)
    if count_tokens(joined) > budget:  # pragma: no cover - would need tens of thousands of pages
        logger.warning("insights_notes_truncated", document_id=str(document.id))
        joined = split_by_tokens(joined, budget)[0]
    logger.info("insights_map_reduce", document_id=str(document.id), notes=len(notes), levels=levels)
    return joined


def generate_insights(db: Session, document: Document, parsed: ParsedDocument) -> DocumentInsight:
    provider = get_provider()
    text = prepare_analysis_text(document, parsed)
    with failed_calls_to(_failure_sink(document), "insights", provider.chat_model, insights_prompt.VERSION):
        result = provider.complete_structured(
            insights_prompt.build_messages(document.title, text), InsightsOutput, max_tokens=2500
        )
    record_usage(
        result.usage,
        operation="insights",
        owner_id=document.owner_id,
        document_id=document.id,
        prompt_version=insights_prompt.VERSION,
    )
    out = result.value
    values = {
        "summary": out.summary,
        "document_type": out.document_type[:100],
        "category": out.category,
        "tags": [t.lower().strip() for t in out.tags][:10],
        "key_points": out.key_points,
        "key_facts": [f.model_dump() for f in out.key_facts],
        "entities": out.entities.model_dump(),
        "sentiment": out.sentiment.model_dump(),
        "suggested_questions": out.suggested_questions[:5],
        "language": out.language[:20],
        "analysis_text": text if needs_condensing(parsed) else None,
        "model": result.usage.model,
        "prompt_version": insights_prompt.VERSION,
    }
    insight = db.get(DocumentInsight, document.id)
    if insight is None:
        insight = DocumentInsight(document_id=document.id, **values)
        db.add(insight)
    else:
        for key, value in values.items():
            setattr(insight, key, value)
    return insight

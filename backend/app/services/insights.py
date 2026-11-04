"""AI document understanding (runs in the worker): summary, category, tags, key facts with pages,
entities, sentiment and suggested questions. Long documents use map-reduce."""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm import get_provider
from app.llm.prompts import insights as insights_prompt
from app.llm.schemas import InsightsOutput, SectionNotes
from app.models import Document, DocumentInsight
from app.services.chunking import count_tokens
from app.services.parsing import ParsedDocument
from app.services.usage import record_usage

logger = get_logger(__name__)

SECTION_TOKENS = 12_000


def _split_sections(parsed: ParsedDocument, max_tokens: int) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    size = 0
    for page in parsed.pages:
        block = f"[Page {page.number}]\n{page.text}" if page.number else page.text
        tokens = count_tokens(block)
        if current and size + tokens > max_tokens:
            sections.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += tokens
    if current:
        sections.append("\n\n".join(current))
    return sections


def prepare_analysis_text(db: Session, document: Document, parsed: ParsedDocument) -> str:
    """Full text when it fits the budget; otherwise condensed section notes (map step)."""
    settings = get_settings()
    text = parsed.text_with_page_markers
    if count_tokens(text) <= settings.insights_max_input_tokens:
        return text

    provider = get_provider()
    notes: list[str] = []
    for section in _split_sections(parsed, SECTION_TOKENS):
        messages = insights_prompt.build_section_notes_messages(document.title, section)
        result = provider.complete_structured(messages, SectionNotes, model=provider.fast_model, max_tokens=1500)
        record_usage(
            db,
            result.usage,
            operation="insights_section_notes",
            owner_id=document.owner_id,
            document_id=document.id,
            prompt_version=insights_prompt.VERSION,
        )
        notes.append(result.value.notes)
    logger.info("insights_map_reduce", document_id=str(document.id), sections=len(notes))
    return "\n\n".join(notes)


def generate_insights(db: Session, document: Document, parsed: ParsedDocument) -> DocumentInsight:
    provider = get_provider()
    text = prepare_analysis_text(db, document, parsed)
    result = provider.complete_structured(
        insights_prompt.build_messages(document.title, text), InsightsOutput, max_tokens=2500
    )
    record_usage(
        db,
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

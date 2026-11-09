"""Grounded multi-document chat (RAG).

Flow for every question:
    1. Rewrite a follow-up into a standalone question using recent history (fast model) - only
       when it refers back to the conversation ("what does IT cover?", "and for fire?").
    2. Answer cache lookup (exact: same documents + same standalone question; then similar meaning).
    3. Embed the question (cached) -> hybrid retrieval over the session's ready documents
       (documents not ready yet are reported as skipped_documents).
    4. Drop weak evidence; if nothing relevant remains, abstain without calling the model.
    5. Generate the answer with numbered sources, then map [n] citations back to chunks.
    6. Suggest follow-up questions the documents can answer - generated together with step 5; once
       the answer is known, the 3 that add the most beyond it are shown.
Both the blocking and the streaming (SSE) endpoints share these steps. If an answer fails or the
client disconnects, the question, any partial answer and the calls already paid for are still saved.
"""

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anyio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, NotReadyError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.llm import get_provider
from app.llm.base import Message, Usage
from app.llm.prompts import chat as chat_prompt
from app.llm.prompts import followup_check as followup_check_prompt
from app.llm.prompts import followups as followups_prompt
from app.llm.prompts import rewrite as rewrite_prompt
from app.llm.schemas import AnsweredSuggestions, FollowUpQuestions
from app.models import ChatMessage, ChatSession, Document, DocumentInsight, User
from app.models.enums import DocumentStatus, MessageRole
from app.services import cache, events
from app.services.chunking import count_tokens
from app.services.documents import get_owned_documents
from app.services.retrieval import RetrievedChunk, hybrid_search
from app.services.usage import UsageEntry, add_entries, failed_calls_to

logger = get_logger(__name__)

CITATION_RE = re.compile(r"\[(\d{1,2})\]")
SNIPPET_CHARS = 300


# --------------------------------------------------------------------------- sessions
async def create_session(db: AsyncSession, user: User, document_ids: list[uuid.UUID], title: str | None) -> ChatSession:
    unique_ids = list(dict.fromkeys(document_ids))
    # Documents may still be processing: the chat can be opened now and answers use whichever are
    # ready at question time (the others are reported as skipped_documents).
    documents = await get_owned_documents(db, user, unique_ids)
    if not title:
        title = documents[0].title if len(documents) == 1 else f"{documents[0].title} + {len(documents) - 1} more"
    session = ChatSession(owner_id=user.id, title=title[:255], documents=documents)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    await events.apublish(user.id, "chat.session_created", {"session_id": session.id})
    return session


async def get_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> ChatSession:
    session = (
        await db.execute(select(ChatSession).where(ChatSession.id == session_id, ChatSession.owner_id == user.id))
    ).scalar_one_or_none()
    if session is None:
        raise NotFoundError("Chat session not found.", code="session_not_found")
    return session


async def list_sessions(db: AsyncSession, user: User, limit: int, offset: int) -> tuple[list[ChatSession], int]:
    base = select(ChatSession).where(ChatSession.owner_id == user.id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(ChatSession.updated_at.desc()).limit(limit).offset(offset))).scalars().all()
    return list(rows), total


async def message_counts(db: AsyncSession, session_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not session_ids:
        return {}
    rows = await db.execute(
        select(ChatMessage.session_id, func.count())
        .where(ChatMessage.session_id.in_(session_ids))
        .group_by(ChatMessage.session_id)
    )
    return {sid: count for sid, count in rows.all()}


async def get_messages(db: AsyncSession, session_id: uuid.UUID, limit: int | None = None) -> list[ChatMessage]:
    stmt = select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at.desc())
    if limit:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(reversed(rows))


async def delete_session(db: AsyncSession, user: User, session_id: uuid.UUID) -> None:
    session = await get_session(db, user, session_id)
    await db.delete(session)
    await db.commit()
    await events.apublish(user.id, "chat.session_deleted", {"session_id": session_id})


# --------------------------------------------------------------------------- preparation
@dataclass
class PreparedQuestion:
    question: str
    standalone_question: str
    documents: list[Document]
    history: list[Message]
    chunks: list[RetrievedChunk] = field(default_factory=list)
    sources: list[chat_prompt.SourceChunk] = field(default_factory=list)
    cache_key: str = ""
    semantic_index_key: str = ""
    embedding: list[float] | None = None
    cached: dict[str, Any] | None = None
    cache_match: dict[str, Any] | None = None  # how a cached answer was found (exact / similar question)
    skipped_documents: list[dict[str, Any]] = field(default_factory=list)  # in the chat but not ready
    retrieval_ms: int = 0
    usages: list[UsageEntry] = field(default_factory=list)  # every call made (or avoided by a cache)


def _history_messages(messages: list[ChatMessage]) -> list[Message]:
    return [{"role": m.role, "content": m.content[:2000]} for m in messages]


async def _embed_query(text: str, prepared: PreparedQuestion) -> list[float]:
    provider = get_provider()
    cached = await cache.aget_cached_embedding(provider.embedding_model, text)
    if cached is not None:
        saved = Usage(provider.embedding_model, prompt_tokens=count_tokens(text))
        prepared.usages.append(UsageEntry("embed_query", saved, cached=True))
        return cached
    with failed_calls_to(prepared.usages.append, "embed_query", provider.embedding_model):
        result = await provider.aembed([text])
    prepared.usages.append(UsageEntry("embed_query", result.usage))
    await cache.aset_cached_embedding(provider.embedding_model, text, result.vectors[0])
    return result.vectors[0]


# Words and openings that only make sense with the conversation ("what does IT cover?", "and for
# fire?"). A follow-up without any of them is already standalone, so the rewrite call is skipped.
_REFERRING = re.compile(
    r"\b(it|its|it's|they|them|their|theirs|this|that|these|those|he|she|him|her|his|hers|one|ones|"
    r"former|latter|above|previous|same|other|else|also|too|instead|there|then|so)\b",
    re.IGNORECASE,
)
_CONTINUATION = re.compile(r"^\s*(and|or|but|what about|how about|why|why not|ok|okay)\b", re.IGNORECASE)
SHORT_FOLLOW_UP_WORDS = 4  # "for fire?", "the deductible?" - too short to stand alone


def needs_rewrite(question: str) -> bool:
    words = re.findall(r"[\w'$%.-]+", question)
    return (
        len(words) <= SHORT_FOLLOW_UP_WORDS or bool(_CONTINUATION.search(question)) or bool(_REFERRING.search(question))
    )


def _is_relevant(chunk: RetrievedChunk, threshold: float) -> bool:
    return chunk.keyword_match or chunk.similarity >= threshold


async def prepare_question(
    db: AsyncSession,
    user: User,
    session: ChatSession,
    question: str,
    use_cache: bool = True,
    usages: list[UsageEntry] | None = None,
) -> PreparedQuestion:
    settings = get_settings()
    provider = get_provider()
    question = question.strip()

    ready = [d for d in session.documents if d.status == DocumentStatus.READY]
    skipped = [
        {"document_id": str(d.id), "title": d.title, "status": d.status}
        for d in session.documents
        if d.status != DocumentStatus.READY
    ]
    if not session.documents:  # every document of this chat was deleted since it started
        raise ConflictError(
            "The documents in this chat were deleted. Start a new chat to ask about other documents.",
            code="chat_documents_deleted",
        )
    if not ready:
        raise NotReadyError(
            "None of the documents in this chat are ready yet. Wait for processing to finish.",
            details={"statuses": {str(d.id): d.status for d in session.documents}},
        )

    previous = await get_messages(db, session.id, limit=settings.chat_history_turns * 2)
    prepared = PreparedQuestion(
        question=question, standalone_question=question, documents=ready, history=_history_messages(previous)
    )
    prepared.skipped_documents = skipped
    if usages is not None:
        prepared.usages = usages  # the caller's list, so it still sees the calls made if a later step fails

    if prepared.history and needs_rewrite(question):
        with failed_calls_to(prepared.usages.append, "rewrite_query", provider.fast_model, rewrite_prompt.VERSION):
            rewrite = await provider.acomplete(
                rewrite_prompt.build_messages(prepared.history, question), model=provider.fast_model, max_tokens=200
            )
        prepared.usages.append(UsageEntry("rewrite_query", rewrite.usage, rewrite_prompt.VERSION))
        prepared.standalone_question = rewrite.text.strip() or question

    versions = [f"{d.id}:{d.processed_at.isoformat() if d.processed_at else ''}" for d in ready]
    # A cached entry holds the answer AND its follow-ups, so a change to either prompt starts fresh.
    prompts = f"{chat_prompt.VERSION}+{followups_prompt.VERSION}+{followup_check_prompt.VERSION}"
    prepared.cache_key = cache.answer_cache_key(str(user.id), versions, prepared.standalone_question, prompts)
    prepared.semantic_index_key = cache.semantic_index_key(str(user.id), versions, prompts)
    if use_cache:
        prepared.cached = await cache.aget_json(prepared.cache_key)
        if prepared.cached is not None:
            prepared.cache_match = {"type": "exact", "question": prepared.standalone_question, "similarity": 1.0}
            return prepared

    start = time.perf_counter()
    embedding = await _embed_query(prepared.standalone_question, prepared)
    prepared.embedding = embedding
    if use_cache:
        match = await cache.afind_semantic(prepared.semantic_index_key, prepared.standalone_question, embedding)
        if match is not None:
            answer_key, matched_question, similarity = match
            prepared.cached = await cache.aget_json(answer_key)
            if prepared.cached is not None:
                prepared.cache_match = {
                    "type": "similar",
                    "question": matched_question,
                    "similarity": round(similarity, 3),
                }
                return prepared

    chunks = await hybrid_search(
        db,
        owner_id=user.id,
        document_ids=[d.id for d in ready],
        query=prepared.standalone_question,
        embedding=embedding,
    )
    prepared.retrieval_ms = int((time.perf_counter() - start) * 1000)
    prepared.chunks = [c for c in chunks if _is_relevant(c, settings.min_relevance_score)]
    titles = {d.id: d.title for d in ready}
    prepared.sources = [
        chat_prompt.SourceChunk(i + 1, titles[c.document_id], c.page_number, c.content)
        for i, c in enumerate(prepared.chunks)
    ]
    return prepared


# --------------------------------------------------------------------------- post-processing
QUOTED_RE = re.compile(r"[\"“]([^\"“”\n]{1,200}?)[\"”]")


def _normalise_for_match(text: str) -> str:
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    return " ".join(text.lower().split())


def unquote_computed_values(answer: str, source_texts: list[str]) -> str:
    """Remove quotation marks from quoted figures that are not in the sources.

    Prompt rule 7 says never to put a computed value in quotation marks, but the model still
    sometimes writes a total as if quoted ('a total of "$18,650"' when the quote only says
    "$18,400 plus a $250 policy fee"). Quotation marks claim the source said it, so the code
    enforces the rule: a quoted span with a number that does not appear in any source keeps its
    text but loses the marks. Quotes without numbers are left as written."""
    haystack = _normalise_for_match(" ".join(source_texts))

    def check(match: re.Match[str]) -> str:
        inner = match.group(1)
        if not any(ch.isdigit() for ch in inner):
            return match.group(0)
        plain = CITATION_RE.sub("", inner).replace("*", "")  # bold and [n] marks are not source text
        needle = _normalise_for_match(plain).strip().rstrip(".,;:")
        return match.group(0) if needle and needle in haystack else inner

    return QUOTED_RE.sub(check, answer)


def extract_citations(answer: str, prepared: PreparedQuestion) -> list[dict[str, Any]]:
    """Map [n] markers in the answer back to the retrieved chunks (ignores invented numbers).

    A "not found" answer often says what the documents DO say ("the broker is Bayline Risk Partners
    [1][2], but no address is given"); those markers are mapped too, so they can be opened like any
    other citation. The answer still counts as abstained, not grounded (see _save_exchange)."""
    titles = {d.id: d.title for d in prepared.documents}
    used: list[int] = []
    for match in CITATION_RE.finditer(answer):
        n = int(match.group(1))
        if 1 <= n <= len(prepared.chunks) and n not in used:
            used.append(n)
    citations = []
    for n in sorted(used):
        chunk = prepared.chunks[n - 1]
        snippet = " ".join(chunk.content.split())
        citations.append(
            {
                "number": n,
                "document_id": str(chunk.document_id),
                "document_title": titles[chunk.document_id],
                "page_number": chunk.page_number,
                "chunk_index": chunk.chunk_index,
                "snippet": snippet[:SNIPPET_CHARS] + ("…" if len(snippet) > SNIPPET_CHARS else ""),
                "similarity": round(chunk.similarity, 4),
            }
        )
    return citations


async def _insight_questions(db: AsyncSession, documents: list[Document]) -> list[str]:
    rows = (
        (
            await db.execute(
                select(DocumentInsight.suggested_questions).where(
                    DocumentInsight.document_id.in_([d.id for d in documents])
                )
            )
        )
        .scalars()
        .all()
    )
    return [q for questions in rows for q in (questions or [])][:10]


async def _llm_follow_ups(prepared: PreparedQuestion) -> list[str]:
    """Follow-up questions from the question and the top sources. Needs no DB session and not the
    answer, so it runs concurrently with answer generation. Best effort: failure -> no suggestions."""
    provider = get_provider()
    top = prepared.sources[: followups_prompt.MAX_SOURCES]
    try:
        with failed_calls_to(prepared.usages.append, "follow_ups", provider.fast_model, followups_prompt.VERSION):
            result = await provider.acomplete_structured(
                followups_prompt.build_messages(prepared.standalone_question, chat_prompt.format_sources(top)),
                FollowUpQuestions,
                model=provider.fast_model,
                max_tokens=200,
            )
    except Exception:
        logger.warning("follow_ups_failed", exc_info=True)
        return []
    prepared.usages.append(UsageEntry("follow_ups", result.usage, followups_prompt.VERSION))
    return [q.strip() for q in result.value.questions if q.strip()][: followups_prompt.CANDIDATES]


FOLLOW_UPS_SHOWN = 3
# The embedding half of the repeat check (the model check is the other half): a suggestion this close
# to one line of the answer restates it. Measured with text-embedding-3-small on the walkthrough's
# answers (9 Nov): genuine follow-ups scored at most 0.63 against their closest line; the one repeat
# the model check misses ("Who is the Facilities Manager to notify within 24 hours?") scored 0.69.
# Comparing with the whole answer (the first version) hid repeats inside long answers.
FOLLOW_UP_LINE_SIMILARITY = 0.66
# A suggestion this close to a question the user already asked in this chat re-asks it. Measured on
# the 9 Nov walkthrough: re-asks scored 0.68-0.69 against the earlier question, others at most 0.58.
FOLLOW_UP_ASKED_SIMILARITY = 0.60
MAX_ANSWER_LINES = 12
EXTRA_SUGGESTIONS = 5  # from the documents' insights, used when too few model suggestions are new

_CITATION_MARK = re.compile(r"\[\d+\]")


def answer_lines(answer: str) -> list[str]:
    """The answer's facts, one per line or sentence, without citation marks or bullets; headings
    ("Property policy deductibles:") and fragments under 4 words are left out."""
    lines: list[str] = []
    for raw in answer.splitlines():
        text = _CITATION_MARK.sub("", raw).strip(" -*\u2022\t")
        if text.endswith(":"):
            continue  # a heading ("General liability quote deductible:"), not a fact
        lines += [s.strip() for s in re.split(r"(?<=[.;])\s+", text) if len(s.split()) >= 4]
    return lines[:MAX_ANSWER_LINES] or [answer]


def _earlier_answers(prepared: PreparedQuestion) -> list[str]:
    """This chat's previous answers, most recent first."""
    return [str(m["content"]) for m in reversed(prepared.history) if m["role"] == MessageRole.ASSISTANT]


async def _answered_by_model(prepared: PreparedQuestion, answer: str, suggestions: list[str]) -> set[int] | None:
    """Indexes of the suggestions the fast model says this answer, or an earlier answer in the chat,
    already covers; None if the call failed."""
    provider = get_provider()
    try:
        with failed_calls_to(
            prepared.usages.append, "check_follow_ups", provider.fast_model, followup_check_prompt.VERSION
        ):
            result = await provider.acomplete_structured(
                followup_check_prompt.build_messages(answer, suggestions, _earlier_answers(prepared)),
                AnsweredSuggestions,
                model=provider.fast_model,
                max_tokens=200,
            )
    except Exception:
        logger.warning("follow_ups_check_failed", exc_info=True)
        return None
    prepared.usages.append(UsageEntry("check_follow_ups", result.usage, followup_check_prompt.VERSION))
    return {n - 1 for n in result.value.answered if 1 <= n <= len(suggestions)}


async def _closeness(
    prepared: PreparedQuestion, answer: str, suggestions: list[str]
) -> tuple[list[float], list[float]] | None:
    """One embedding call. For each suggestion: its similarity to the closest line of the answer, and
    to the closest question the user already asked in this chat. None if the call failed."""
    provider = get_provider()
    lines = answer_lines(answer)
    asked = list(dict.fromkeys(_asked_questions(prepared)))
    try:
        with failed_calls_to(prepared.usages.append, "embed_follow_ups", provider.embedding_model):
            result = await provider.aembed([*lines, *asked, *suggestions])
    except Exception:
        logger.warning("follow_ups_rank_failed", exc_info=True)
        return None
    prepared.usages.append(UsageEntry("embed_follow_ups", result.usage))
    line_vectors = result.vectors[: len(lines)]
    asked_vectors = result.vectors[len(lines) : len(lines) + len(asked)]
    vectors = result.vectors[len(lines) + len(asked) :]
    to_lines = [max(cache.cosine(line, v) for line in line_vectors) for v in vectors]
    to_asked = [max((cache.cosine(q, v) for q in asked_vectors), default=0.0) for v in vectors]
    return to_lines, to_asked


async def pick_follow_ups(
    prepared: PreparedQuestion, answer: str, candidates: list[str], extras: list[str] | None = None
) -> list[str]:
    """Up to 3 suggestions that ask for something the answer doesn't already say.

    The model's candidates were written in parallel with the answer, so some ask for facts the answer
    then states, or that the conversation already covered. Two checks run at the same time (~1 s,
    after the answer): the fast model reads the answer (and the chat's earlier answers) and lists the
    suggestions they cover; an embedding call scores each suggestion against the closest line of the
    answer and the closest question already asked. A suggestion any check flags is dropped. Of the rest, the
    model's candidates come first (least like the answer first), then `extras` (the documents' own
    suggested questions, checked the same way) fill any gap. Shown in their original order.
    If both checks fail, the first 3 candidates are shown unchecked.
    """
    suggestions = list(dict.fromkeys([*candidates, *(extras or [])]))
    if not suggestions:
        return []
    answered, scores = await asyncio.gather(
        _answered_by_model(prepared, answer, suggestions), _closeness(prepared, answer, suggestions)
    )
    if answered is None and scores is None:
        return suggestions[:FOLLOW_UPS_SHOWN]

    def repeats(i: int) -> bool:
        if answered is not None and i in answered:
            return True
        if scores is not None:
            to_line, to_asked = scores[0][i], scores[1][i]
            return to_line >= FOLLOW_UP_LINE_SIMILARITY or to_asked >= FOLLOW_UP_ASKED_SIMILARITY
        return False

    new = [i for i in range(len(suggestions)) if not repeats(i)]
    from_model = [i for i in new if i < len(candidates)]
    if scores is not None:
        from_model.sort(key=lambda i: scores[0][i])
    chosen = (from_model + [i for i in new if i >= len(candidates)])[:FOLLOW_UPS_SHOWN]
    return [suggestions[i] for i in sorted(chosen)]


def _start_follow_ups(prepared: PreparedQuestion) -> "asyncio.Task[list[str]] | None":
    """Started right before the answer call; None when there is nothing to base suggestions on."""
    return asyncio.create_task(_llm_follow_ups(prepared)) if prepared.sources else None


_WORD = re.compile(r"[a-z0-9$%]+")
_FILLER = {
    "a",
    "an",
    "the",
    "is",
    "are",
    "was",
    "be",
    "do",
    "does",
    "did",
    "what",
    "which",
    "who",
    "whom",
    "when",
    "where",
    "why",
    "how",
    "for",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "and",
    "or",
    "if",
    "it",
    "this",
    "that",
    "there",
    "any",
    "must",
    "should",
    "can",
    "i",
    "we",
    "you",
    "my",
    "our",
    "with",
    "from",
    "after",
    "before",
}


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _FILLER}


def _already_asked(suggestion: str, asked: list[str]) -> bool:
    """True if the suggestion asks something the user already asked in this chat: every content word
    of one is in the other ("How long must damaged stock be kept?" vs "How long must damaged stock be
    kept for insurer inspection?")."""
    words = _content_words(suggestion)
    for question in asked:
        other = _content_words(question)
        if len(other) >= 2 and (other <= words or words <= other):
            return True
    return False


def _asked_questions(prepared: PreparedQuestion) -> list[str]:
    earlier = [str(m["content"]) for m in prepared.history if m["role"] == MessageRole.USER]
    return [*earlier, prepared.question, prepared.standalone_question]


async def _finish_follow_ups(
    db: AsyncSession,
    prepared: PreparedQuestion,
    task: "asyncio.Task[list[str]] | None",
    answer: str,
    abstained: bool,
) -> list[str]:
    asked = _asked_questions(prepared)
    extras = [q for q in await _insight_questions(db, prepared.documents) if not _already_asked(q, asked)]
    if abstained or task is None:
        if task is not None:
            await task  # already paid for; let it finish so its usage is recorded, but don't show it
        # "Not found": suggest what the documents DO cover, minus what this chat already asked or answered.
        return await pick_follow_ups(prepared, answer, [], extras[:EXTRA_SUGGESTIONS])
    candidates = [q for q in await task if not _already_asked(q, asked)]
    return await pick_follow_ups(prepared, answer, candidates, extras[:EXTRA_SUGGESTIONS])


def _cached_follow_ups(prepared: PreparedQuestion) -> list[str]:
    """A reused answer brings the suggestions saved with it; drop any this chat has already asked
    (a word-level check, so a cached answer stays instant, with no AI call)."""
    assert prepared.cached is not None
    asked = _asked_questions(prepared)
    return [q for q in prepared.cached["follow_ups"] if not _already_asked(q, asked)]


def _abstain_text() -> str:
    return f"{chat_prompt.ABSTAIN_PREFIX} Try rephrasing, or add a document that covers this topic."


async def _save_exchange(
    db: AsyncSession,
    user: User,
    session: ChatSession,
    prepared: PreparedQuestion,
    *,
    answer: str,
    citations: list[dict[str, Any]],
    follow_ups: list[str],
    answer_usage: Usage | None,
    from_cache: bool,
    started: float,
) -> tuple[ChatMessage, ChatMessage]:
    abstained = answer.strip().startswith(chat_prompt.ABSTAIN_PREFIX)
    user_message = ChatMessage(session_id=session.id, role=MessageRole.USER, content=prepared.question)
    db.add(user_message)
    await db.flush()  # ensures the user message sorts before the answer
    assistant = ChatMessage(
        session_id=session.id,
        role=MessageRole.ASSISTANT,
        content=answer,
        standalone_question=prepared.standalone_question,
        citations=citations,
        follow_up_questions=follow_ups,
        retrieval={
            "chunks": [
                {
                    "document_id": str(c.document_id),
                    "chunk_index": c.chunk_index,
                    "page_number": c.page_number,
                    "similarity": round(c.similarity, 4),
                    "keyword_match": c.keyword_match,
                    "score": c.score,
                }
                for c in prepared.chunks
            ],
            "retrieval_ms": prepared.retrieval_ms,
            "cache_match": prepared.cache_match,
            "skipped_documents": prepared.skipped_documents,
        },
        grounded=bool(citations) and not abstained,
        abstained=abstained,
        from_cache=from_cache,
        model=answer_usage.model if answer_usage else None,
        prompt_tokens=answer_usage.prompt_tokens if answer_usage else None,
        completion_tokens=answer_usage.completion_tokens if answer_usage else None,
        latency_ms=int((time.perf_counter() - started) * 1000),
    )
    db.add(assistant)
    add_entries(db, prepared.usages, owner_id=user.id, session_id=session.id)  # same transaction as the messages
    session.updated_at = utcnow()
    await db.commit()
    await db.refresh(user_message)
    await db.refresh(assistant)
    await events.apublish(user.id, "chat.message_created", {"session_id": session.id, "message_id": assistant.id})
    return user_message, assistant


async def _cache_answer(
    prepared: PreparedQuestion,
    answer: str,
    citations: list[dict[str, Any]],
    follow_ups: list[str],
    answer_usage: Usage | None = None,
) -> None:
    if answer.strip().startswith(chat_prompt.ABSTAIN_PREFIX):
        return  # don't pin a "not found" answer; a new document version might answer it
    await cache.aset_json(
        prepared.cache_key,
        {
            "answer": answer,
            "citations": citations,
            "follow_ups": follow_ups,
            # What a cache hit avoids paying for (recorded as a cached=true usage row on each hit).
            "usage": answer_usage.__dict__ if answer_usage else None,
            "chunks": [
                c.__dict__ | {"chunk_id": str(c.chunk_id), "document_id": str(c.document_id)} for c in prepared.chunks
            ],
        },
        get_settings().answer_cache_ttl_seconds,
    )
    if prepared.embedding is not None:
        await cache.aadd_semantic_entry(
            prepared.semantic_index_key, prepared.standalone_question, prepared.embedding, prepared.cache_key
        )


def _restore_cached_chunks(prepared: PreparedQuestion) -> None:
    assert prepared.cached is not None
    if saved := prepared.cached.get("usage"):
        prepared.usages.append(UsageEntry("chat_answer", Usage(**saved), chat_prompt.VERSION, cached=True))
    prepared.chunks = [
        RetrievedChunk(**(c | {"chunk_id": uuid.UUID(c["chunk_id"]), "document_id": uuid.UUID(c["document_id"])}))
        for c in prepared.cached.get("chunks", [])
    ]


# --------------------------------------------------------------------------- interrupted answers
INTERRUPTED_NOTE = "(The answer was interrupted.)"
FAILED_TEXT = "The answer could not be generated. Please ask again."


@dataclass
class _Attempt:
    """What an answer attempt has done so far; kept outside prepare_question so a failure at any
    step (or the user closing the tab mid-stream) can still record the calls already paid for."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    question: str
    usages: list[UsageEntry] = field(default_factory=list)
    standalone_question: str | None = None
    answer_messages: list[Message] | None = None  # set when the answer call starts
    answer_parts: list[str] = field(default_factory=list)
    answer_usage: Usage | None = None  # set when the provider reports final usage
    final_answer: str = ""
    saved: bool = False


async def _save_interrupted(attempt: _Attempt, reason: str) -> None:
    """Persist a failed or interrupted answer: the question, whatever part of the answer was
    produced, and every AI call made (the interrupted answer call as success=false, its tokens
    estimated locally because the provider only reports usage at the end of a stream).

    Uses its own DB session: the request's session may be mid-transaction or already closed."""
    if attempt.saved or (not attempt.usages and attempt.answer_messages is None):
        return  # nothing was spent and no answer started (e.g. no document is ready): nothing to keep
    from app.db.session import get_async_sessionmaker

    async with get_async_sessionmaker()() as db:
        if await db.get(ChatSession, attempt.session_id) is None:
            return  # the chat itself was deleted meanwhile
        partial = "".join(attempt.answer_parts).strip()
        db.add(ChatMessage(session_id=attempt.session_id, role=MessageRole.USER, content=attempt.question))
        await db.flush()
        db.add(
            ChatMessage(
                session_id=attempt.session_id,
                role=MessageRole.ASSISTANT,
                content=f"{partial}\n\n{INTERRUPTED_NOTE}" if partial else FAILED_TEXT,
                standalone_question=attempt.standalone_question,
                citations=[],
                follow_up_questions=[],
                retrieval={"interrupted": True, "reason": reason},
                grounded=False,
                abstained=False,
                from_cache=False,
            )
        )
        add_entries(db, attempt.usages, owner_id=attempt.user_id, session_id=attempt.session_id)
        if attempt.answer_messages is not None and attempt.answer_usage is None:
            # The answer call itself failed or was cut off. The provider reports usage only at the end
            # of a stream, so estimate locally: nothing generated -> nothing billed; otherwise the
            # prompt plus the tokens streamed so far.
            estimate = Usage(model=get_provider().chat_model)
            if partial:
                estimate.prompt_tokens = sum(count_tokens(str(m["content"])) for m in attempt.answer_messages)
                estimate.completion_tokens = count_tokens(partial)
            add_entries(
                db,
                [UsageEntry("chat_answer", estimate, chat_prompt.VERSION, success=False)],
                owner_id=attempt.user_id,
                session_id=attempt.session_id,
            )
        await db.commit()
    attempt.saved = True
    logger.warning("chat_answer_interrupted", session_id=str(attempt.session_id), reason=reason)


async def _save_interrupted_shielded(attempt: _Attempt, reason: str) -> None:
    # Shielded: when the client disconnects the stream is cancelled, and without the shield this
    # save would be cancelled at its first await.
    with anyio.CancelScope(shield=True):
        try:
            await _save_interrupted(attempt, reason)
        except Exception:
            logger.exception("chat_interrupted_save_failed", session_id=str(attempt.session_id))


# --------------------------------------------------------------------------- ask (blocking)
async def ask(
    db: AsyncSession, user: User, session: ChatSession, question: str, use_cache: bool = True
) -> tuple[ChatMessage, ChatMessage]:
    attempt = _Attempt(user_id=user.id, session_id=session.id, question=question.strip())
    try:
        return await _ask(db, user, session, question, use_cache, attempt)
    except BaseException as exc:  # includes cancellation
        await _save_interrupted_shielded(attempt, type(exc).__name__)
        raise


async def _ask(
    db: AsyncSession, user: User, session: ChatSession, question: str, use_cache: bool, attempt: _Attempt
) -> tuple[ChatMessage, ChatMessage]:
    started = time.perf_counter()
    prepared = await prepare_question(db, user, session, question, use_cache, usages=attempt.usages)
    attempt.standalone_question = prepared.standalone_question

    if prepared.cached is not None:
        _restore_cached_chunks(prepared)
        return await _save_exchange(
            db,
            user,
            session,
            prepared,
            answer=prepared.cached["answer"],
            citations=prepared.cached["citations"],
            follow_ups=_cached_follow_ups(prepared),
            answer_usage=None,
            from_cache=True,
            started=started,
        )

    answer_usage: Usage | None = None
    follow_ups_task = _start_follow_ups(prepared)  # runs while the answer is generated
    try:
        if not prepared.sources:
            answer = _abstain_text()
        else:
            provider = get_provider()
            attempt.answer_messages = chat_prompt.build_messages(
                question, prepared.standalone_question, prepared.sources, prepared.history
            )
            completion = await provider.acomplete(  # failure: see _save_interrupted
                attempt.answer_messages, reasoning_effort=get_settings().chat_reasoning_effort or None
            )
            answer, answer_usage = completion.text.strip(), completion.usage
            answer = unquote_computed_values(answer, [s.content for s in prepared.sources])
            attempt.answer_usage = answer_usage
            prepared.usages.append(UsageEntry("chat_answer", completion.usage, chat_prompt.VERSION))
    except BaseException:
        if follow_ups_task is not None:
            follow_ups_task.cancel()
        raise

    citations = extract_citations(answer, prepared)
    abstained = answer.startswith(chat_prompt.ABSTAIN_PREFIX)
    follow_ups = await _finish_follow_ups(db, prepared, follow_ups_task, answer, abstained)
    await _cache_answer(prepared, answer, citations, follow_ups, answer_usage)
    return await _save_exchange(
        db,
        user,
        session,
        prepared,
        answer=answer,
        citations=citations,
        follow_ups=follow_ups,
        answer_usage=answer_usage,
        from_cache=False,
        started=started,
    )


# --------------------------------------------------------------------------- ask (streaming)
async def ask_stream(
    db: AsyncSession, user: User, session: ChatSession, question: str, use_cache: bool = True
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Yields (event, data) pairs: `status`, many `token`, then `done`.

    If anything fails, or the client disconnects mid-answer, the question, the partial answer and the
    AI calls already made are still saved (see _save_interrupted); the error itself propagates."""
    attempt = _Attempt(user_id=user.id, session_id=session.id, question=question.strip())
    try:
        async for item in _ask_stream(db, user, session, question, use_cache, attempt):
            yield item
    finally:
        if not attempt.saved:
            await _save_interrupted_shielded(attempt, "stream_interrupted")


async def _ask_stream(
    db: AsyncSession, user: User, session: ChatSession, question: str, use_cache: bool, attempt: _Attempt
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    started = time.perf_counter()
    yield "status", {"stage": "retrieving"}
    prepared = await prepare_question(db, user, session, question, use_cache, usages=attempt.usages)
    attempt.standalone_question = prepared.standalone_question
    yield (
        "status",
        {
            "stage": "generating",
            "standalone_question": prepared.standalone_question,
            "skipped_documents": prepared.skipped_documents,
        },
    )

    from_cache = prepared.cached is not None
    follow_ups_task = None if from_cache else _start_follow_ups(prepared)  # runs while tokens stream
    try:
        async for item in _generate(prepared, question, attempt, from_cache):
            yield item
    except BaseException:
        if follow_ups_task is not None:
            follow_ups_task.cancel()
        raise
    answer, answer_usage = attempt.final_answer, attempt.answer_usage

    if from_cache:
        assert prepared.cached is not None
        citations, follow_ups = prepared.cached["citations"], _cached_follow_ups(prepared)
    else:
        citations = extract_citations(answer, prepared)
        abstained = answer.startswith(chat_prompt.ABSTAIN_PREFIX)
        follow_ups = await _finish_follow_ups(db, prepared, follow_ups_task, answer, abstained)
        await _cache_answer(prepared, answer, citations, follow_ups, answer_usage)

    user_message, assistant = await _save_exchange(
        db,
        user,
        session,
        prepared,
        answer=answer,
        citations=citations,
        follow_ups=follow_ups,
        answer_usage=answer_usage,
        from_cache=from_cache,
        started=started,
    )
    attempt.saved = True
    yield "done", {"question": user_message, "answer": assistant}


async def _generate(
    prepared: PreparedQuestion, question: str, attempt: _Attempt, from_cache: bool
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """The answer itself: from the cache, a fixed "not found" text, or streamed from the model.
    Leaves the full text in attempt.final_answer and the provider's usage in attempt.answer_usage."""
    if from_cache:
        assert prepared.cached is not None
        _restore_cached_chunks(prepared)
        answer = prepared.cached["answer"]
        yield "token", {"text": answer}
    elif not prepared.sources:
        answer = _abstain_text()
        yield "token", {"text": answer}
    else:
        provider = get_provider()
        attempt.answer_messages = chat_prompt.build_messages(
            question, prepared.standalone_question, prepared.sources, prepared.history
        )
        async for event in provider.astream(
            attempt.answer_messages, reasoning_effort=get_settings().chat_reasoning_effort or None
        ):
            if event.delta:
                attempt.answer_parts.append(event.delta)
                yield "token", {"text": event.delta}
            if event.usage:
                attempt.answer_usage = event.usage
        answer = unquote_computed_values("".join(attempt.answer_parts).strip(), [s.content for s in prepared.sources])
        if attempt.answer_usage:
            prepared.usages.append(UsageEntry("chat_answer", attempt.answer_usage, chat_prompt.VERSION))

    attempt.final_answer = answer

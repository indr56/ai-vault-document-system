"""Grounded multi-document chat (RAG).

Flow for every question:
    1. Rewrite a follow-up into a standalone question using recent history (fast model).
    2. Answer cache lookup (same documents + same standalone question).
    3. Embed the question (cached) -> hybrid retrieval over the session's ready documents.
    4. Drop weak evidence; if nothing relevant remains, abstain without calling the model.
    5. Generate the answer with numbered sources, then map [n] citations back to chunks.
    6. Suggest follow-up questions the documents can answer.
Both the blocking and the streaming (SSE) endpoints share steps 1-4 and 5's post-processing.
"""

import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError, NotReadyError
from app.core.logging import get_logger
from app.db.base import utcnow
from app.llm import get_provider
from app.llm.base import Message, Usage
from app.llm.prompts import chat as chat_prompt
from app.llm.prompts import followups as followups_prompt
from app.llm.prompts import rewrite as rewrite_prompt
from app.llm.schemas import FollowUpQuestions
from app.models import ChatMessage, ChatSession, Document, DocumentInsight, User
from app.models.enums import DocumentStatus, MessageRole
from app.services import cache
from app.services.documents import get_ready_documents
from app.services.retrieval import RetrievedChunk, hybrid_search
from app.services.usage import arecord_usage

logger = get_logger(__name__)

CITATION_RE = re.compile(r"\[(\d{1,2})\]")
SNIPPET_CHARS = 300


# --------------------------------------------------------------------------- sessions
async def create_session(db: AsyncSession, user: User, document_ids: list[uuid.UUID], title: str | None) -> ChatSession:
    unique_ids = list(dict.fromkeys(document_ids))
    documents = await get_ready_documents(db, user, unique_ids)
    if not title:
        title = documents[0].title if len(documents) == 1 else f"{documents[0].title} + {len(documents) - 1} more"
    session = ChatSession(owner_id=user.id, title=title[:255], documents=documents)
    db.add(session)
    await db.commit()
    await db.refresh(session)
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
    cached: dict[str, Any] | None = None
    retrieval_ms: int = 0
    usages: list[tuple[str, Usage, str | None]] = field(default_factory=list)  # (operation, usage, prompt_version)


def _history_messages(messages: list[ChatMessage]) -> list[Message]:
    return [{"role": m.role, "content": m.content[:2000]} for m in messages]


async def _embed_query(text: str, prepared: PreparedQuestion) -> list[float]:
    provider = get_provider()
    cached = await cache.aget_cached_embedding(provider.embedding_model, text)
    if cached is not None:
        return cached
    result = await provider.aembed([text])
    prepared.usages.append(("embed_query", result.usage, None))
    await cache.aset_cached_embedding(provider.embedding_model, text, result.vectors[0])
    return result.vectors[0]


def _is_relevant(chunk: RetrievedChunk, threshold: float) -> bool:
    return chunk.keyword_match or chunk.similarity >= threshold


async def prepare_question(
    db: AsyncSession, user: User, session: ChatSession, question: str, use_cache: bool = True
) -> PreparedQuestion:
    settings = get_settings()
    provider = get_provider()
    question = question.strip()

    ready = [d for d in session.documents if d.status == DocumentStatus.READY]
    if not ready:
        raise NotReadyError(
            "None of the documents in this chat are ready yet. Wait for processing to finish.",
            details={"statuses": {str(d.id): d.status for d in session.documents}},
        )

    previous = await get_messages(db, session.id, limit=settings.chat_history_turns * 2)
    prepared = PreparedQuestion(
        question=question, standalone_question=question, documents=ready, history=_history_messages(previous)
    )

    if prepared.history:
        rewrite = await provider.acomplete(
            rewrite_prompt.build_messages(prepared.history, question), model=provider.fast_model, max_tokens=200
        )
        prepared.usages.append(("rewrite_query", rewrite.usage, rewrite_prompt.VERSION))
        prepared.standalone_question = rewrite.text.strip() or question

    versions = [f"{d.id}:{d.processed_at.isoformat() if d.processed_at else ''}" for d in ready]
    prepared.cache_key = cache.answer_cache_key(
        str(user.id), versions, prepared.standalone_question, chat_prompt.VERSION
    )
    prepared.cached = await cache.aget_json(prepared.cache_key) if use_cache else None
    if prepared.cached is not None:
        return prepared

    start = time.perf_counter()
    embedding = await _embed_query(prepared.standalone_question, prepared)
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
def extract_citations(answer: str, prepared: PreparedQuestion) -> list[dict[str, Any]]:
    """Map [n] markers in the answer back to the retrieved chunks (ignores invented numbers)."""
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
    return [q for questions in rows for q in (questions or [])][:3]


async def suggest_follow_ups(db: AsyncSession, prepared: PreparedQuestion, answer: str, abstained: bool) -> list[str]:
    """Best effort: a failure here must never fail the answer."""
    if abstained or not prepared.sources:
        return await _insight_questions(db, prepared.documents)
    provider = get_provider()
    try:
        result = await provider.acomplete_structured(
            followups_prompt.build_messages(
                prepared.standalone_question, answer, chat_prompt.format_sources(prepared.sources)
            ),
            FollowUpQuestions,
            model=provider.fast_model,
            max_tokens=200,
        )
    except Exception:
        logger.warning("follow_ups_failed", exc_info=True)
        return []
    prepared.usages.append(("follow_ups", result.usage, followups_prompt.VERSION))
    return [q.strip() for q in result.value.questions if q.strip()][:3]


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
    for operation, usage, prompt_version in prepared.usages:
        await arecord_usage(
            db, usage, operation=operation, owner_id=user.id, session_id=session.id, prompt_version=prompt_version
        )
    session.updated_at = utcnow()
    await db.commit()
    await db.refresh(user_message)
    await db.refresh(assistant)
    return user_message, assistant


async def _cache_answer(
    prepared: PreparedQuestion, answer: str, citations: list[dict[str, Any]], follow_ups: list[str]
) -> None:
    if answer.strip().startswith(chat_prompt.ABSTAIN_PREFIX):
        return  # don't pin a "not found" answer; a new document version might answer it
    await cache.aset_json(
        prepared.cache_key,
        {
            "answer": answer,
            "citations": citations,
            "follow_ups": follow_ups,
            "chunks": [
                c.__dict__ | {"chunk_id": str(c.chunk_id), "document_id": str(c.document_id)} for c in prepared.chunks
            ],
        },
        get_settings().answer_cache_ttl_seconds,
    )


def _restore_cached_chunks(prepared: PreparedQuestion) -> None:
    assert prepared.cached is not None
    prepared.chunks = [
        RetrievedChunk(**(c | {"chunk_id": uuid.UUID(c["chunk_id"]), "document_id": uuid.UUID(c["document_id"])}))
        for c in prepared.cached.get("chunks", [])
    ]


# --------------------------------------------------------------------------- ask (blocking)
async def ask(
    db: AsyncSession, user: User, session: ChatSession, question: str, use_cache: bool = True
) -> tuple[ChatMessage, ChatMessage]:
    started = time.perf_counter()
    prepared = await prepare_question(db, user, session, question, use_cache)

    if prepared.cached is not None:
        _restore_cached_chunks(prepared)
        return await _save_exchange(
            db,
            user,
            session,
            prepared,
            answer=prepared.cached["answer"],
            citations=prepared.cached["citations"],
            follow_ups=prepared.cached["follow_ups"],
            answer_usage=None,
            from_cache=True,
            started=started,
        )

    answer_usage: Usage | None = None
    if not prepared.sources:
        answer = _abstain_text()
    else:
        provider = get_provider()
        completion = await provider.acomplete(
            chat_prompt.build_messages(question, prepared.standalone_question, prepared.sources, prepared.history)
        )
        answer, answer_usage = completion.text.strip(), completion.usage
        prepared.usages.append(("chat_answer", completion.usage, chat_prompt.VERSION))

    citations = extract_citations(answer, prepared)
    abstained = answer.startswith(chat_prompt.ABSTAIN_PREFIX)
    follow_ups = await suggest_follow_ups(db, prepared, answer, abstained)
    await _cache_answer(prepared, answer, citations, follow_ups)
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
    """Yields (event, data) pairs: `status`, `sources`, many `token`, then `done` (or `error`)."""
    started = time.perf_counter()
    yield "status", {"stage": "retrieving"}
    prepared = await prepare_question(db, user, session, question, use_cache)
    yield "status", {"stage": "generating", "standalone_question": prepared.standalone_question}

    answer_usage: Usage | None = None
    from_cache = prepared.cached is not None
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
        parts: list[str] = []
        async for event in provider.astream(
            chat_prompt.build_messages(question, prepared.standalone_question, prepared.sources, prepared.history)
        ):
            if event.delta:
                parts.append(event.delta)
                yield "token", {"text": event.delta}
            if event.usage:
                answer_usage = event.usage
        answer = "".join(parts).strip()
        if answer_usage:
            prepared.usages.append(("chat_answer", answer_usage, chat_prompt.VERSION))

    if from_cache:
        assert prepared.cached is not None
        citations, follow_ups = prepared.cached["citations"], prepared.cached["follow_ups"]
    else:
        citations = extract_citations(answer, prepared)
        follow_ups = await suggest_follow_ups(db, prepared, answer, answer.startswith(chat_prompt.ABSTAIN_PREFIX))
        await _cache_answer(prepared, answer, citations, follow_ups)

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
    yield "done", {"question": user_message, "answer": assistant}

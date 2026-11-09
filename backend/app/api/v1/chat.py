import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.core.errors import AppError, NotFoundError, error_responses
from app.core.logging import get_logger
from app.core.rate_limit import rate_limited
from app.db.session import get_async_sessionmaker, get_db
from app.models import ChatMessage, ChatSession, User
from app.schemas.chat import (
    AskRequest,
    AskResponse,
    ChatHistory,
    MessageOut,
    SessionCreate,
    SessionDocument,
    SessionList,
    SessionOut,
)
from app.services import chat as service

logger = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]
ChatUser = Annotated[User, Depends(rate_limited("chat"))]


def _session_out(session: ChatSession, message_count: int) -> SessionOut:
    return SessionOut(
        id=session.id,
        title=session.title,
        documents=[SessionDocument.model_validate(d) for d in session.documents],
        message_count=message_count,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _message_out(message: ChatMessage) -> MessageOut:
    out = MessageOut.model_validate(message)
    out.cache_match = (message.retrieval or {}).get("cache_match")
    return out


@router.post(
    "/sessions",
    responses=error_responses(404),
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a conversation about one or more documents",
)
async def create_session(body: SessionCreate, db: DB, user: CurrentUser) -> SessionOut:
    session = await service.create_session(db, user, body.document_ids, body.title)
    return _session_out(session, 0)


@router.get("/sessions", response_model=SessionList, summary="List your conversations, most recent first")
async def list_sessions(
    db: DB,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SessionList:
    sessions, total = await service.list_sessions(db, user, limit, offset)
    counts = await service.message_counts(db, [s.id for s in sessions])
    return SessionList(items=[_session_out(s, counts.get(s.id, 0)) for s in sessions], total=total)


@router.get("/sessions/{session_id}", response_model=ChatHistory, summary="Conversation history")
async def get_history(session_id: uuid.UUID, db: DB, user: CurrentUser) -> ChatHistory:
    session = await service.get_session(db, user, session_id)
    messages = await service.get_messages(db, session.id)
    return ChatHistory(session=_session_out(session, len(messages)), messages=[_message_out(m) for m in messages])


@router.delete(
    "/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a conversation and its messages"
)
async def delete_session(session_id: uuid.UUID, db: DB, user: CurrentUser) -> Response:
    await service.delete_session(db, user, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/sessions/{session_id}/messages",
    responses=error_responses(409, 503),
    response_model=AskResponse,
    summary="Ask a question (waits for the full answer)",
    description="Answers only from the session's documents, with [n] citations mapped to document, "
    "page and snippet. If the documents don't contain the answer, `abstained` is true.",
)
async def ask(session_id: uuid.UUID, body: AskRequest, db: DB, user: ChatUser) -> AskResponse:
    session = await service.get_session(db, user, session_id)
    question, answer = await service.ask(db, user, session, body.question, body.use_cache)
    return AskResponse(question=_message_out(question), answer=_message_out(answer))


@router.post(
    "/sessions/{session_id}/messages/stream",
    responses=error_responses(409, 503),
    summary="Ask a question and stream the answer (Server-Sent Events)",
    description="Events: `status` → `token` (many) → `done` (the saved question and answer, same shape "
    "as the non-streaming endpoint) or `error`.",
    response_class=EventSourceResponse,
)
async def ask_stream(session_id: uuid.UUID, body: AskRequest, db: DB, user: ChatUser) -> EventSourceResponse:
    # Validate ownership before the stream starts so a 404 is a normal HTTP error.
    await service.get_session(db, user, session_id)
    user_id = user.id
    # Give the request's connection back to the pool now. FastAPI would only close this session after
    # the whole stream has been sent, so it would sit idle for the seconds the answer takes while the
    # stream below holds a second connection.
    await db.close()

    async def events() -> AsyncIterator[dict[str, Any]]:
        # The stream body runs after this endpoint has returned, so it opens its own session.
        async with get_async_sessionmaker()() as stream_db:
            try:
                # Inside the try: if the chat was deleted between the check above and now, the client
                # gets an `error` event instead of an empty 200 response.
                stream_user = await stream_db.get(User, user_id)
                if stream_user is None:
                    raise NotFoundError("User not found.", code="user_not_found")
                session = await service.get_session(stream_db, stream_user, session_id)
                async for event, data in service.ask_stream(
                    stream_db, stream_user, session, body.question, body.use_cache
                ):
                    if event == "done":
                        data = AskResponse(
                            question=_message_out(data["question"]), answer=_message_out(data["answer"])
                        ).model_dump(mode="json")
                    yield {"event": event, "data": json.dumps(data, default=str)}
            except AppError as exc:
                yield {"event": "error", "data": json.dumps({"code": exc.code, "message": exc.message})}
            except Exception:
                logger.exception("chat_stream_failed", session_id=str(session_id))
                yield {
                    "event": "error",
                    "data": json.dumps({"code": "internal_error", "message": "The answer could not be generated."}),
                }

    return EventSourceResponse(events(), ping=15)

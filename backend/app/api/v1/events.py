"""Real-time status updates over WebSocket.

Workers publish events (document.processing / stage / ready / failed / insights_ready,
comparison.completed, chat.* ...) to a per-owner Redis channel; this endpoint relays them.

Authentication: non-browser clients send the X-API-Key header. Browsers can't set headers on a
WebSocket, so they first call POST /events/ticket (normal header auth) and connect with
`?ticket=<ticket>`: a random, single-use value that expires after 60 seconds. The API key itself
never appears in a URL, so it can't leak into access or proxy logs.
"""

import asyncio
import contextlib
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from app.core.errors import UnauthorizedError
from app.core.logging import get_logger
from app.core.rate_limit import rate_limited
from app.core.security import authenticate
from app.db.session import get_async_sessionmaker
from app.models import User
from app.schemas.api_keys import EventsTicket
from app.services.cache import get_aredis
from app.services.events import channel_for

logger = get_logger(__name__)
router = APIRouter(tags=["events"])
CurrentUser = Annotated[User, Depends(rate_limited("default"))]


TICKET_TTL_SECONDS = 60


def _ticket_key(ticket: str) -> str:
    return f"ws-ticket:{ticket}"


@router.post(
    "/events/ticket",
    response_model=EventsTicket,
    summary="Get a single-use ticket for the events WebSocket (for browsers)",
)
async def create_ticket(user: CurrentUser) -> EventsTicket:
    ticket = secrets.token_urlsafe(32)
    await get_aredis().set(_ticket_key(ticket), str(user.id), ex=TICKET_TTL_SECONDS)
    return EventsTicket(ticket=ticket, expires_in=TICKET_TTL_SECONDS)


async def _websocket_user_id(websocket: WebSocket) -> uuid.UUID:
    raw_key = websocket.headers.get("x-api-key")
    if raw_key:
        async with get_async_sessionmaker()() as db:
            return (await authenticate(db, raw_key)).id
    ticket = websocket.query_params.get("ticket")
    if ticket:
        user_id = await get_aredis().getdel(_ticket_key(ticket))  # GETDEL: a ticket works exactly once
        if user_id:
            return uuid.UUID(user_id.decode() if isinstance(user_id, bytes) else user_id)
    raise UnauthorizedError("Send X-API-Key, or a ticket from POST /events/ticket.")


@router.websocket("/events")
async def events(websocket: WebSocket) -> None:
    try:
        user_id = await _websocket_user_id(websocket)
    except UnauthorizedError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    pubsub = get_aredis().pubsub()
    await pubsub.subscribe(channel_for(user_id))
    await websocket.send_json({"type": "connected"})

    async def relay() -> None:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"].decode())

    async def wait_for_close() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await websocket.receive_text()  # clients may send pings; we ignore content

    tasks = [asyncio.create_task(relay()), asyncio.create_task(wait_for_close())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await pubsub.unsubscribe()
        await pubsub.aclose()

"""Real-time status updates over WebSocket.

Workers publish events (document.processing / stage / ready / failed / insights_ready,
comparison.completed ...) to a per-owner Redis channel; this endpoint relays them to the browser.
Browsers can't set headers on WebSockets, so the API key may also be passed as `?api_key=`.
"""

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.errors import UnauthorizedError
from app.core.logging import get_logger
from app.core.security import authenticate
from app.db.session import get_async_sessionmaker
from app.services.cache import get_aredis
from app.services.events import channel_for

logger = get_logger(__name__)
router = APIRouter(tags=["events"])


@router.websocket("/events")
async def events(websocket: WebSocket) -> None:
    raw_key = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    try:
        async with get_async_sessionmaker()() as db:
            user = await authenticate(db, raw_key)
    except UnauthorizedError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    pubsub = get_aredis().pubsub()
    await pubsub.subscribe(channel_for(user.id))
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

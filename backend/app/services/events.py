"""Real-time events: workers publish status changes to Redis pub/sub; the WebSocket endpoint
forwards them to connected clients of the same owner."""

import contextlib
import json
import uuid
from typing import Any

from app.services.cache import get_aredis, get_redis


def channel_for(owner_id: uuid.UUID | str) -> str:
    return f"events:{owner_id}"


def publish(owner_id: uuid.UUID | str, event_type: str, payload: dict[str, Any]) -> None:
    """Best effort: a failed publish must never break document processing."""
    with contextlib.suppress(Exception):
        get_redis().publish(channel_for(owner_id), json.dumps({"type": event_type, **payload}, default=str))


async def apublish(owner_id: uuid.UUID | str, event_type: str, payload: dict[str, Any]) -> None:
    """Async version for API code: publishing must not block the event loop (the sync client
    does a network round trip). Best effort, like publish()."""
    with contextlib.suppress(Exception):
        await get_aredis().publish(channel_for(owner_id), json.dumps({"type": event_type, **payload}, default=str))

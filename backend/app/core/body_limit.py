"""Cap request body size before anything reads it.

FastAPI parses a multipart form (spooling every file to temporary disk) *before* it runs
dependencies such as API-key auth and rate limits, so a per-file size check in the handler comes
too late: a 20 GB body would fill the disk first. This middleware runs ahead of all that and
rejects a body that is, or turns out to be, larger than the route can ever accept.
"""

from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from fastapi import HTTPException, status

from app.core.config import get_settings

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

MULTIPART_OVERHEAD = 1024 * 1024  # form boundaries, headers and other fields around the file(s)
JSON_BODY_LIMIT = 1024 * 1024  # every other endpoint takes a small JSON body


def body_limit_for(path: str) -> int:
    settings = get_settings()
    if path.endswith("/documents/batch"):
        return settings.max_upload_bytes * settings.max_batch_files + MULTIPART_OVERHEAD
    if path.endswith("/documents"):
        return settings.max_upload_bytes + MULTIPART_OVERHEAD
    return JSON_BODY_LIMIT


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail=f"The request body is larger than this endpoint accepts ({limit // (1024 * 1024)} MB).",
    )


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        limit = body_limit_for(scope["path"])

        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await _send_413(scope, send, limit)  # refused before a single byte of the body is read
            return

        received = 0

        async def counting_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:  # no or a false Content-Length: stop as soon as the limit is passed
                    raise _too_large(limit)
            return message

        await self.app(scope, counting_receive, send)


async def _send_413(scope: Scope, send: Send, limit: int) -> None:
    import json

    body = json.dumps(
        {
            "error": {
                "code": "payload_too_large",
                "message": _too_large(limit).detail,
                "details": {"max_bytes": limit},
                "request_id": None,
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})

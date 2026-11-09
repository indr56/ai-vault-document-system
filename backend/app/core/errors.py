"""Application errors and a single, consistent JSON error envelope.

Every error response looks like:
    {"error": {"code": "document_not_found", "message": "...", "details": {...}, "request_id": "..."}}
"""

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class ValidationFailedError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_failed"


class PayloadTooLargeError(AppError):
    status_code = status.HTTP_413_CONTENT_TOO_LARGE
    code = "file_too_large"


class UnsupportedMediaError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_file_type"


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class QuotaExceededError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "quota_exceeded"


class NotReadyError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "document_not_ready"


class AIServiceError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "ai_service_unavailable"


class QueueUnavailableError(AppError):
    """The background-job broker could not accept the job; nothing was started."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "queue_unavailable"


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable code, e.g. document_not_found")
    message: str = Field(description="Human-readable explanation")
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = Field(default=None, description="Also in the X-Request-ID response header")


class ErrorResponse(BaseModel):
    """Every error the API returns has this shape."""

    error: ErrorBody


ERROR_DESCRIPTIONS = {
    401: "Missing or invalid API key (X-API-Key header).",
    404: "Not found, or not yours.",
    409: "Conflicts with the current state, e.g. the document is still processing.",
    413: "The request body or a file is larger than allowed.",
    415: "Unsupported file type, or the content doesn't match the extension.",
    422: "The request failed validation.",
    429: "Rate limit or daily AI token quota exceeded; see Retry-After.",
    500: "Unexpected server error.",
    503: "A dependency is unavailable (the job queue or the AI provider); retry shortly.",
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    """For a route's `responses=`: documents these error codes in /docs with the shared schema."""
    return {code: {"model": ErrorResponse, "description": ERROR_DESCRIPTIONS[code]} for code in codes}


HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
}


def _envelope(request: Request, code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": getattr(request.state, "request_id", None),
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        headers = {}
        if isinstance(exc, RateLimitedError) and "retry_after_seconds" in exc.details:
            headers["Retry-After"] = str(exc.details["retry_after_seconds"])
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, exc.code, exc.message, exc.details),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=_envelope(request, "validation_failed", "Request validation failed", {"errors": exc.errors()}),
        )

    # Errors raised by the framework itself (unknown route, wrong method, unreadable body, the body
    # size limit) get the same envelope as ours, so clients parse one error format.
    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = HTTP_ERROR_CODES.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(request, code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error", path=request.url.path)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_envelope(request, "internal_error", "An unexpected error occurred"),
        )

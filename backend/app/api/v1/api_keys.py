import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import error_responses
from app.core.rate_limit import rate_limited
from app.db.session import get_db
from app.models import User
from app.schemas.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyOut
from app.services import api_keys as service

router = APIRouter(prefix="/api-keys", tags=["api keys"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]


@router.post(
    "",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (the full key is shown only in this response)",
)
async def create_api_key(body: ApiKeyCreate, db: DB, user: CurrentUser) -> ApiKeyCreated:
    key, raw = await service.create_key(db, user, body.name)
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=raw)


@router.get("", response_model=list[ApiKeyOut], summary="List your API keys (prefix, status, last used)")
async def list_api_keys(db: DB, user: CurrentUser) -> list[ApiKeyOut]:
    return [ApiKeyOut.model_validate(k) for k in await service.list_keys(db, user)]


@router.delete(
    "/{key_id}",
    responses=error_responses(409),
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (not the one making this request)",
)
async def revoke_api_key(key_id: uuid.UUID, request: Request, db: DB, user: CurrentUser) -> Response:
    await service.revoke_key(db, user, key_id, getattr(request.state, "api_key_id", None))
    return Response(status_code=status.HTTP_204_NO_CONTENT)

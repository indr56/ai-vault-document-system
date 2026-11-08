"""API-key authentication. Clients send `X-API-Key: <key>`; keys are stored hashed (SHA-256)."""

import hashlib
import secrets
from datetime import timedelta

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.errors import UnauthorizedError
from app.db.base import utcnow
from app.db.session import get_db
from app.models import ApiKey, User

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Your API key")


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Used by POST /api-keys. 32 random bytes; the "vault_" prefix makes leaked keys easy to scan for."""
    return f"vault_{secrets.token_urlsafe(32)}"


async def authenticate(db: AsyncSession, raw_key: str | None) -> User:
    return (await authenticate_key(db, raw_key)).user


async def authenticate_key(db: AsyncSession, raw_key: str | None) -> ApiKey:
    if not raw_key:
        raise UnauthorizedError("Missing API key. Send it in the X-API-Key header.")
    api_key = (
        await db.execute(
            select(ApiKey)
            .options(selectinload(ApiKey.user))
            .where(ApiKey.key_hash == hash_api_key(raw_key), ApiKey.is_active.is_(True))
        )
    ).scalar_one_or_none()
    if api_key is None:
        raise UnauthorizedError("Invalid API key.")
    # Shown in GET /api-keys so a user can spot unused keys before revoking them.
    # Throttled to one write a minute so authentication doesn't write on every request.
    if api_key.last_used_at is None or utcnow() - api_key.last_used_at > timedelta(minutes=1):
        api_key.last_used_at = utcnow()
        await db.commit()
    return api_key


async def get_current_user(
    request: Request,
    raw_key: str | None = Depends(api_key_header),
    db: AsyncSession = Depends(get_db),
) -> User:
    api_key = await authenticate_key(db, raw_key)
    request.state.user_id = api_key.user_id
    request.state.api_key_id = api_key.id  # lets DELETE /api-keys refuse to revoke the key in use
    return api_key.user


async def ensure_demo_user(db: AsyncSession) -> None:
    """Idempotently create the demo user and API key configured in DEMO_API_KEY."""
    settings = get_settings()
    raw_key = settings.demo_api_key.get_secret_value()
    if not raw_key:
        return
    key_hash = hash_api_key(raw_key)
    if (await db.execute(select(ApiKey.id).where(ApiKey.key_hash == key_hash))).first():
        return
    user = (await db.execute(select(User).where(User.email == settings.demo_user_email))).scalar_one_or_none()
    if user is None:
        user = User(email=settings.demo_user_email)
        db.add(user)
        await db.flush()
    db.add(ApiKey(user_id=user.id, name="demo", key_hash=key_hash, key_prefix=raw_key[:8]))
    await db.commit()

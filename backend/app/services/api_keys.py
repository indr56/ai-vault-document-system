"""API key management: a user creates extra keys (e.g. one per integration), lists them, revokes them.

The raw key is returned once at creation; only its SHA-256 hash is stored. Revoking is a soft delete
(is_active = false), so past usage stays attributable to the key.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.security import generate_api_key, hash_api_key
from app.models import ApiKey, User

PREFIX_CHARS = 10  # "vault_Ab3d" - enough to recognise a key in a list, useless to an attacker


async def create_key(db: AsyncSession, user: User, name: str) -> tuple[ApiKey, str]:
    raw = generate_api_key()
    key = ApiKey(user_id=user.id, name=name.strip(), key_hash=hash_api_key(raw), key_prefix=raw[:PREFIX_CHARS])
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key, raw


async def list_keys(db: AsyncSession, user: User) -> list[ApiKey]:
    rows = await db.execute(select(ApiKey).where(ApiKey.user_id == user.id).order_by(ApiKey.created_at.desc()))
    return list(rows.scalars())


async def revoke_key(db: AsyncSession, user: User, key_id: uuid.UUID, current_key_id: uuid.UUID | None) -> None:
    key = (await db.execute(select(ApiKey).where(ApiKey.id == key_id, ApiKey.user_id == user.id))).scalar_one_or_none()
    if key is None:
        raise NotFoundError("API key not found.", code="api_key_not_found")
    if key.id == current_key_id:
        # Revoking the key that made this request would lock the caller out mid-session.
        raise ConflictError("You can't revoke the key used for this request; use another key.", code="key_in_use")
    key.is_active = False
    await db.commit()

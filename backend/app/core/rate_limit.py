"""Per-user rate limiting (fixed one-minute windows in Redis) and a daily AI token quota.

Per user, not per API key: a user can create several keys (/api-keys), and creating more keys
must not multiply their limits or quota."""

import time
from datetime import UTC, datetime, timedelta

from fastapi import Depends
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import QuotaExceededError, RateLimitedError
from app.core.logging import get_logger
from app.core.security import get_current_user
from app.db.session import get_db
from app.models import AIUsage, User
from app.services.cache import get_aredis

logger = get_logger(__name__)


async def check_rate_limit(user_id: str, bucket: str, limit: int) -> None:
    window = int(time.time() // 60)
    key = f"rl:{bucket}:{user_id}:{window}"
    try:
        # INCR and EXPIRE in one MULTI/EXEC transaction: there is no moment where the counter exists
        # without an expiry (a crash between two separate calls left a key that never expired).
        async with get_aredis().pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 70)
            count, _ = await pipe.execute()
    except RedisError as exc:
        # Fail open: the limiter protects the service, it must not take it down. Without Redis every
        # request would otherwise be a 500. Logged so the outage is visible.
        logger.warning("rate_limiter_unavailable", bucket=bucket, error=str(exc))
        return
    if count > limit:
        raise RateLimitedError(
            f"Rate limit exceeded: {limit} requests per minute for '{bucket}'.",
            details={"limit_per_minute": limit, "retry_after_seconds": 60 - int(time.time() % 60)},
        )


async def check_daily_token_quota(db: AsyncSession, user: User) -> None:
    quota = get_settings().daily_token_quota
    since = datetime.now(UTC) - timedelta(days=1)
    used = (
        await db.execute(
            select(func.coalesce(func.sum(AIUsage.total_tokens), 0)).where(
                AIUsage.owner_id == user.id,
                AIUsage.created_at >= since,
                AIUsage.cached.is_(False),  # tokens a cache saved are not spent
            )
        )
    ).scalar_one()
    if used >= quota:
        raise QuotaExceededError(
            "Daily AI token quota reached. Try again later.", details={"used": int(used), "quota": quota}
        )


def rate_limited(bucket: str):  # type: ignore[no-untyped-def]
    """Dependency factory: `Depends(rate_limited("chat"))` returns the authenticated user."""
    settings = get_settings()
    limits = {
        "default": settings.rate_limit_per_minute,
        "upload": settings.upload_rate_limit_per_minute,
        "chat": settings.chat_rate_limit_per_minute,
    }

    async def dependency(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> User:
        await check_rate_limit(str(user.id), bucket, limits[bucket])
        if bucket in ("upload", "chat"):
            await check_daily_token_quota(db, user)
        return user

    return dependency

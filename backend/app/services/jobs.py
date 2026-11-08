"""Shared plumbing for background jobs: per-item locks, "already enqueued" markers and retry backoff.

Used by document processing, insights regeneration and comparisons, so all three jobs follow the
same reliability rules.
"""

import random
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import redis

from app.core.logging import get_logger
from app.services import cache

logger = get_logger(__name__)

RETRY_BACKOFF_SECONDS = (10, 30, 90)
JITTER = 0.2  # +-20 %
ENQUEUED_MARKER_TTL_SECONDS = 30 * 60

# Delete the lock only if it still holds OUR token. Without the check, a job that outlived the TTL
# would delete the lock a second job took in the meantime, letting a third job start.
_RELEASE_LOCK = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class TransientJobError(Exception):
    """Worth retrying (network / rate limit / provider outage)."""


@contextmanager
def redis_lock(key: str, ttl_seconds: int) -> Iterator[bool]:
    """Yields True if this job owns the lock. The lock value is a random token, released atomically."""
    token = uuid.uuid4().hex
    client = cache.get_redis()
    acquired = bool(client.set(key, token, nx=True, ex=ttl_seconds))
    try:
        yield acquired
    finally:
        if acquired:
            try:
                client.eval(_RELEASE_LOCK, 1, key, token)
            except redis.RedisError:
                logger.warning("lock_release_failed", key=key)  # it expires on its own (TTL)


def retry_countdown(retries: int) -> int:
    """10 s, 30 s, 90 s ... each +-20 %, so jobs that failed together don't all retry in the same second."""
    base = RETRY_BACKOFF_SECONDS[min(retries, len(RETRY_BACKOFF_SECONDS) - 1)]
    return max(1, round(base * random.uniform(1 - JITTER, 1 + JITTER)))


# ----------------------------------------------------------------------------- enqueued markers
# Set when a job message is sent, removed when a worker picks it up. While the marker exists the item
# is known to be waiting in the broker, so the requeue safety net does not send it again. It expires,
# so an item whose message was really lost (e.g. the broker restarted empty) is still requeued later.
def _marker(kind: str, item_id: uuid.UUID | str) -> str:
    return f"enqueued:{kind}:{item_id}"


def mark_enqueued(kind: str, item_id: uuid.UUID | str) -> None:
    try:
        cache.get_redis().set(_marker(kind, item_id), "1", ex=ENQUEUED_MARKER_TTL_SECONDS)
    except redis.RedisError:
        logger.warning("enqueued_marker_failed", kind=kind, item_id=str(item_id))


def clear_enqueued(kind: str, item_id: uuid.UUID | str) -> None:
    try:
        cache.get_redis().delete(_marker(kind, item_id))
    except redis.RedisError:
        logger.warning("enqueued_marker_failed", kind=kind, item_id=str(item_id))


def waiting_in_queue(kind: str, item_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Which of these items still have a message waiting in the broker."""
    if not item_ids:
        return set()
    flags = cache.get_redis().mget([_marker(kind, i) for i in item_ids])
    return {i for i, flag in zip(item_ids, flags, strict=True) if flag}

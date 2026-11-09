"""Redis helpers: embedding cache (avoid paying twice for the same text) and answer cache.

The caches are an optimisation, never a dependency: if Redis is down or slow, every read behaves
as a miss and every write is skipped (logged as a warning), so uploads and chat keep working, they
just cost a little more until Redis is back.
"""

import array
import base64
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from functools import lru_cache, wraps
from typing import Any, ParamSpec, TypeVar

import redis
import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


def fail_open(default: Any) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """On a Redis error, log and return `default` (a miss / no-op) instead of raising."""

    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return fn(*args, **kwargs)
            except redis.RedisError as exc:
                logger.warning("cache_unavailable", operation=fn.__name__, error=str(exc))
                return default(*args, **kwargs) if callable(default) else default  # type: ignore[no-any-return]

        return wrapper

    return decorator


def afail_open(default: Any) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await fn(*args, **kwargs)
            except redis.RedisError as exc:
                logger.warning("cache_unavailable", operation=fn.__name__, error=str(exc))
                return default  # type: ignore[no-any-return]

        return wrapper

    return decorator


@lru_cache
def get_redis() -> redis.Redis:
    # Timeouts so a hung Redis turns into a quick, handled error instead of a stuck request/job.
    return redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=5)


@lru_cache
def get_aredis() -> aioredis.Redis:
    # No read timeout here: the events WebSocket blocks on pub/sub reads for as long as it is open.
    return aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def embedding_key(model: str, text: str) -> str:
    return f"emb:{model}:{_hash(text)}"


def pack_vector(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack_vector(data: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(data)
    return values.tolist()


@fail_open(default=lambda model, texts: [None] * len(texts))
def get_cached_embeddings(model: str, texts: list[str]) -> list[list[float] | None]:
    raw = get_redis().mget([embedding_key(model, t) for t in texts])
    return [unpack_vector(bytes(r)) if r else None for r in raw]  # type: ignore[arg-type]


@fail_open(default=None)
def set_cached_embeddings(model: str, items: dict[str, list[float]]) -> None:
    ttl = get_settings().embedding_cache_ttl_seconds
    pipe = get_redis().pipeline()
    for text, vector in items.items():
        pipe.set(embedding_key(model, text), pack_vector(vector), ex=ttl)
    pipe.execute()


@afail_open(default=None)
async def aget_cached_embedding(model: str, text: str) -> list[float] | None:
    raw = await get_aredis().get(embedding_key(model, text))
    return unpack_vector(bytes(raw)) if raw else None  # type: ignore[arg-type]


@afail_open(default=None)
async def aset_cached_embedding(model: str, text: str, vector: list[float]) -> None:
    await get_aredis().set(
        embedding_key(model, text), pack_vector(vector), ex=get_settings().embedding_cache_ttl_seconds
    )


def answer_cache_key(owner_id: str, document_versions: list[str], question: str, prompt_version: str) -> str:
    normalized = " ".join(question.lower().split())
    return f"answer:{owner_id}:{_hash(prompt_version, *sorted(document_versions), normalized)}"


@fail_open(default=None)
def get_json(key: str) -> Any | None:
    raw = get_redis().get(key)
    return json.loads(raw) if raw else None  # type: ignore[arg-type]


@fail_open(default=None)
def set_json(key: str, value: Any, ttl: int) -> None:
    get_redis().set(key, json.dumps(value, default=str), ex=ttl)


@afail_open(default=None)
async def aget_json(key: str) -> Any | None:
    raw = await get_aredis().get(key)
    return json.loads(raw) if raw else None


@afail_open(default=None)
async def aset_json(key: str, value: Any, ttl: int) -> None:
    await get_aredis().set(key, json.dumps(value, default=str), ex=ttl)


# --------------------------------------------------------------------------- meaning-based answer cache
# Per (owner, document versions, prompt version) we keep a capped list of answered questions with their
# embeddings. A new question whose embedding is close enough reuses that answer, so paraphrases
# ("flood deductible amount?") and slightly different rewrites still hit the cache.


def semantic_index_key(owner_id: str, document_versions: list[str], prompt_version: str) -> str:
    return f"answer-index:{owner_id}:{_hash(prompt_version, *sorted(document_versions))}"


def cosine(a: list[float], b: list[float]) -> float:
    # OpenAI embeddings are unit-length, so the dot product is the cosine similarity.
    return sum(x * y for x, y in zip(a, b, strict=True))


@afail_open(default=None)
async def aadd_semantic_entry(index_key: str, question: str, embedding: list[float], answer_key: str) -> None:
    settings = get_settings()
    entry = json.dumps({"q": question, "key": answer_key, "e": base64.b64encode(pack_vector(embedding)).decode()})
    redis = get_aredis()
    pipe = redis.pipeline()
    pipe.lpush(index_key, entry)
    pipe.ltrim(index_key, 0, settings.semantic_cache_max_entries - 1)
    pipe.expire(index_key, settings.answer_cache_ttl_seconds)
    await pipe.execute()


_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
_WORD = re.compile(r"[A-Za-z][\w'-]*")


def specifics(question: str) -> frozenset[str]:
    """The details that make two near-identical questions different questions: numbers (years,
    amounts, section numbers) and names (capitalised words after the first word).

    "What is the 2025 premium?" and "What is the 2026 premium?" embed far above the threshold but
    have different answers; so do "...for Acme?" and "...for Northwind?"."""
    numbers = {n.replace(",", "") for n in _NUMBER.findall(question)}
    names = {w.lower() for w in _WORD.findall(question)[1:] if w[0].isupper()}
    return frozenset(numbers | names)


@afail_open(default=None)
async def afind_semantic(index_key: str, question: str, embedding: list[float]) -> tuple[str, str, float] | None:
    """Returns (answer_key, matched_question, similarity) for the closest entry above the threshold
    that asks about the same specifics (numbers and names) as `question`."""
    threshold = get_settings().semantic_cache_threshold
    wanted = specifics(question)
    best: tuple[str, str, float] | None = None
    for raw in await get_aredis().lrange(index_key, 0, -1):
        entry = json.loads(raw)
        if specifics(entry["q"]) != wanted:
            continue
        similarity = cosine(embedding, unpack_vector(base64.b64decode(entry["e"])))
        if similarity >= threshold and (best is None or similarity > best[2]):
            best = (entry["key"], entry["q"], similarity)
    return best

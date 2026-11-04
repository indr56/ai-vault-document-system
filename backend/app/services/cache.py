"""Redis helpers: embedding cache (avoid paying twice for the same text) and answer cache."""

import array
import base64
import hashlib
import json
from functools import lru_cache
from typing import Any

import redis
import redis.asyncio as aioredis

from app.core.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url)


@lru_cache
def get_aredis() -> aioredis.Redis:
    return aioredis.Redis.from_url(get_settings().redis_url)


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


def get_cached_embeddings(model: str, texts: list[str]) -> list[list[float] | None]:
    raw = get_redis().mget([embedding_key(model, t) for t in texts])
    return [unpack_vector(bytes(r)) if r else None for r in raw]  # type: ignore[arg-type]


def set_cached_embeddings(model: str, items: dict[str, list[float]]) -> None:
    ttl = get_settings().embedding_cache_ttl_seconds
    pipe = get_redis().pipeline()
    for text, vector in items.items():
        pipe.set(embedding_key(model, text), pack_vector(vector), ex=ttl)
    pipe.execute()


async def aget_cached_embedding(model: str, text: str) -> list[float] | None:
    raw = await get_aredis().get(embedding_key(model, text))
    return unpack_vector(bytes(raw)) if raw else None  # type: ignore[arg-type]


async def aset_cached_embedding(model: str, text: str, vector: list[float]) -> None:
    await get_aredis().set(
        embedding_key(model, text), pack_vector(vector), ex=get_settings().embedding_cache_ttl_seconds
    )


def answer_cache_key(owner_id: str, document_versions: list[str], question: str, prompt_version: str) -> str:
    normalized = " ".join(question.lower().split())
    return f"answer:{owner_id}:{_hash(prompt_version, *sorted(document_versions), normalized)}"


async def aget_json(key: str) -> Any | None:
    raw = await get_aredis().get(key)
    return json.loads(raw) if raw else None


async def aset_json(key: str, value: Any, ttl: int) -> None:
    await get_aredis().set(key, json.dumps(value, default=str), ex=ttl)


# --------------------------------------------------------------------------- meaning-based answer cache
# Per (owner, document versions, prompt version) we keep a capped list of answered questions with their
# embeddings. A new question whose embedding is close enough reuses that answer, so paraphrases
# ("flood deductible amount?") and slightly different rewrites still hit the cache.


def semantic_index_key(owner_id: str, document_versions: list[str], prompt_version: str) -> str:
    return f"answer-index:{owner_id}:{_hash(prompt_version, *sorted(document_versions))}"


def _cosine(a: list[float], b: list[float]) -> float:
    # OpenAI embeddings are unit-length, so the dot product is the cosine similarity.
    return sum(x * y for x, y in zip(a, b, strict=True))


async def aadd_semantic_entry(index_key: str, question: str, embedding: list[float], answer_key: str) -> None:
    settings = get_settings()
    entry = json.dumps({"q": question, "key": answer_key, "e": base64.b64encode(pack_vector(embedding)).decode()})
    redis = get_aredis()
    pipe = redis.pipeline()
    pipe.lpush(index_key, entry)
    pipe.ltrim(index_key, 0, settings.semantic_cache_max_entries - 1)
    pipe.expire(index_key, settings.answer_cache_ttl_seconds)
    await pipe.execute()


async def afind_semantic(index_key: str, embedding: list[float]) -> tuple[str, str, float] | None:
    """Returns (answer_key, matched_question, similarity) for the closest entry above the threshold."""
    threshold = get_settings().semantic_cache_threshold
    best: tuple[str, str, float] | None = None
    for raw in await get_aredis().lrange(index_key, 0, -1):
        entry = json.loads(raw)
        similarity = _cosine(embedding, unpack_vector(base64.b64decode(entry["e"])))
        if similarity >= threshold and (best is None or similarity > best[2]):
            best = (entry["key"], entry["q"], similarity)
    return best

"""Hybrid retrieval: semantic (pgvector cosine) + keyword (Postgres full-text), fused with
Reciprocal Rank Fusion. Always scoped to the caller's documents.

Why hybrid: embeddings find paraphrases ("water damage" ~ "flood"), while keyword search catches
exact identifiers that embeddings blur (policy numbers, names, "Section 4.2").
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Text, cast, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import DocumentChunk

RRF_K = 60


@dataclass
class RetrievedChunk:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    page_number: int | None
    content: str
    similarity: float  # cosine similarity from the vector search (0 if only keyword-matched)
    score: float  # fused RRF score used for ranking
    keyword_match: bool = False  # contains EVERY word of the question (strict full-text match)


def _fuse(vector_rows: Sequence[Any], keyword_rows: Sequence[Any], top_k: int) -> list[RetrievedChunk]:
    scores: dict[uuid.UUID, float] = {}
    by_id: dict[uuid.UUID, RetrievedChunk] = {}
    for rank, row in enumerate(vector_rows):
        scores[row.id] = scores.get(row.id, 0.0) + 1.0 / (RRF_K + rank + 1)
        by_id[row.id] = RetrievedChunk(
            row.id, row.document_id, row.chunk_index, row.page_number, row.content, float(row.similarity), 0.0
        )
    for rank, row in enumerate(keyword_rows):
        scores[row.id] = scores.get(row.id, 0.0) + 1.0 / (RRF_K + rank + 1)
        chunk = by_id.setdefault(
            row.id, RetrievedChunk(row.id, row.document_id, row.chunk_index, row.page_number, row.content, 0.0, 0.0)
        )
        chunk.keyword_match = bool(row.all_words)
    ranked = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:top_k]
    results = []
    for cid in ranked:
        chunk = by_id[cid]
        chunk.score = round(scores[cid], 6)
        results.append(chunk)
    return results


def _vector_query(owner_id: uuid.UUID, document_ids: list[uuid.UUID], embedding: list[float], limit: int):  # type: ignore[no-untyped-def]
    distance = DocumentChunk.embedding.cosine_distance(embedding)
    return (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.chunk_index,
            DocumentChunk.page_number,
            DocumentChunk.content,
            (1 - distance).label("similarity"),
        )
        .where(DocumentChunk.owner_id == owner_id, DocumentChunk.document_id.in_(document_ids))
        .order_by(distance)
        .limit(limit)
    )


def _any_words_tsquery(query: str):  # type: ignore[no-untyped-def]
    """'What happens if a pipe bursts?' -> 'happen' | 'pipe' | 'burst'.

    plainto_tsquery drops filler words and reduces the rest to roots joined with AND (&);
    swapping & for | means a chunk needs only ONE of the words to be a candidate. ts_rank_cd
    still ranks chunks with more (and closer) words higher. The lexemes are already normalised,
    so the text is cast straight back to tsquery without re-parsing.
    """
    strict_text = cast(func.plainto_tsquery("english", query), Text)
    return cast(func.replace(strict_text, " & ", " | "), TSQUERY)


def _keyword_query(owner_id: uuid.UUID, document_ids: list[uuid.UUID], query: str, limit: int):  # type: ignore[no-untyped-def]
    any_words = _any_words_tsquery(query)  # finds candidates and ranks them
    all_words = func.websearch_to_tsquery("english", query)  # strict: used only for keyword_match
    rank = func.ts_rank_cd(DocumentChunk.tsv, any_words)
    return (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.chunk_index,
            DocumentChunk.page_number,
            DocumentChunk.content,
            rank.label("rank"),
            DocumentChunk.tsv.op("@@")(all_words).label("all_words"),
        )
        .where(
            DocumentChunk.owner_id == owner_id,
            DocumentChunk.document_id.in_(document_ids),
            DocumentChunk.tsv.op("@@")(any_words),
        )
        .order_by(rank.desc())
        .limit(limit)
    )


_TUNE_HNSW = [text("SET LOCAL hnsw.ef_search = 100"), text("SET LOCAL hnsw.iterative_scan = relaxed_order")]


async def hybrid_search(
    db: AsyncSession,
    *,
    owner_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    query: str,
    embedding: list[float],
    top_k: int | None = None,
) -> list[RetrievedChunk]:
    if not document_ids:
        return []
    settings = get_settings()
    candidates = settings.retrieval_candidates
    for stmt in _TUNE_HNSW:  # keep recall high when filtering by document
        await db.execute(stmt)
    vector_rows = (await db.execute(_vector_query(owner_id, document_ids, embedding, candidates))).all()
    keyword_rows = (await db.execute(_keyword_query(owner_id, document_ids, query, candidates))).all()
    return _fuse(vector_rows, keyword_rows, top_k or settings.retrieval_top_k)


def hybrid_search_sync(
    db: Session,
    *,
    owner_id: uuid.UUID,
    document_ids: list[uuid.UUID],
    query: str,
    embedding: list[float],
    top_k: int,
) -> list[RetrievedChunk]:
    candidates = get_settings().retrieval_candidates
    for stmt in _TUNE_HNSW:
        db.execute(stmt)
    vector_rows = db.execute(_vector_query(owner_id, document_ids, embedding, candidates)).all()
    keyword_rows = db.execute(_keyword_query(owner_id, document_ids, query, candidates)).all()
    return _fuse(vector_rows, keyword_rows, top_k)

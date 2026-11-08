import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.models.enums import DocumentStatus, InsightsStatus

EMBEDDING_DIM = get_settings().embedding_dimensions


class Document(UUIDPrimaryKey, Timestamps, Base):
    """An uploaded file and its processing state.

    Versioning: every version is its own row. Rows of the same logical document share
    `version_group_id`; exactly one of them has `is_latest = true`.
    """

    __tablename__ = "documents"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    extension: Mapped[str] = mapped_column(String(10), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=DocumentStatus.QUEUED, nullable=False)
    stage: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    insights_status: Mapped[str] = mapped_column(String(20), default=InsightsStatus.PENDING, nullable=False)
    # Non-fatal problems found while processing, shown on the document: [{"code": ..., "message": ...}]
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"), nullable=False
    )

    page_count: Mapped[int | None] = mapped_column(Integer)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    version_group_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    is_latest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    insight: Mapped["DocumentInsight | None"] = relationship(
        back_populates="document", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        Index("ix_documents_owner_created", "owner_id", "created_at"),
        Index("ix_documents_owner_status", "owner_id", "status"),
        # Unique: enforce duplicate detection and version numbering in the database (races).
        Index("uq_documents_owner_hash", "owner_id", "content_hash", unique=True),
        Index("uq_documents_version_group_version", "version_group_id", "version", unique=True),
        Index(
            "uq_documents_one_latest_per_group",
            "version_group_id",
            unique=True,
            postgresql_where=text("is_latest"),
        ),
    )


class DocumentChunk(UUIDPrimaryKey, Base):
    """A retrieval unit: a slice of one page with its embedding and full-text vector."""

    __tablename__ = "document_chunks"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    # Denormalised owner for fast, safe filtering during retrieval.
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed("to_tsvector('english', content)", persisted=True))

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_index"),
        Index("ix_document_chunks_owner", "owner_id"),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_document_chunks_tsv", "tsv", postgresql_using="gin"),
    )


class DocumentInsight(Timestamps, Base):
    """AI-generated understanding of a document (one row per document)."""

    __tablename__ = "document_insights"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    document_type: Mapped[str | None] = mapped_column(String(100))
    category: Mapped[str | None] = mapped_column(String(100))
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    key_points: Mapped[list[str]] = mapped_column(JSONB, default=list)
    key_facts: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    entities: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    sentiment: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    suggested_questions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    language: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)

    document: Mapped[Document] = relationship(back_populates="insight")


class DocumentSummary(UUIDPrimaryKey, Timestamps, Base):
    """Customised summaries (length / focus / tone), cached per combination."""

    __tablename__ = "document_summaries"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    length: Mapped[str] = mapped_column(String(20), nullable=False)
    tone: Mapped[str] = mapped_column(String(20), nullable=False)
    focus: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (UniqueConstraint("document_id", "length", "tone", "focus", name="uq_document_summaries_variant"),)

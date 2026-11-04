"""Monitoring data: processing runs, AI usage/cost, and API request performance."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey, utcnow
from app.models.enums import ComparisonStatus, RunStatus


class ProcessingRun(UUIDPrimaryKey, Base):
    """One attempt at processing a document (retries create new runs)."""

    __tablename__ = "processing_runs"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    owner_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=RunStatus.RUNNING, nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    stages: Mapped[list["StageRun"]] = relationship(back_populates="run", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_processing_runs_owner_started", "owner_id", "started_at"),
        Index("ix_processing_runs_document", "document_id"),
    )


class StageRun(UUIDPrimaryKey, Base):
    __tablename__ = "processing_stage_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("processing_runs.id", ondelete="CASCADE"))
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ProcessingRun] = relationship(back_populates="stages")

    __table_args__ = (Index("ix_processing_stage_runs_run", "run_id"),)


class AIUsage(UUIDPrimaryKey, Base):
    """Every model call: tokens, latency and estimated cost (USD)."""

    __tablename__ = "ai_usage"

    owner_id: Mapped[uuid.UUID | None] = mapped_column()
    operation: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str | None] = mapped_column(String(20))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    cached: Mapped[bool] = mapped_column(Boolean, default=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column()
    session_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_ai_usage_owner_created", "owner_id", "created_at"),
        Index("ix_ai_usage_operation", "operation"),
    )


class RequestMetric(UUIDPrimaryKey, Base):
    """Per-request API performance data (route template, status, latency)."""

    __tablename__ = "request_metrics"

    method: Mapped[str] = mapped_column(String(10), nullable=False)
    route: Mapped[str] = mapped_column(String(200), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_request_metrics_created", "created_at"),)


class Comparison(UUIDPrimaryKey, Timestamps, Base):
    """AI comparative analysis across 2+ documents (runs asynchronously)."""

    __tablename__ = "comparisons"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    document_ids: Mapped[list[str]] = mapped_column(ARRAY(String(36)), nullable=False)
    focus: Mapped[str] = mapped_column(String(500), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=ComparisonStatus.PENDING, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_message: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_comparisons_owner_created", "owner_id", "created_at"),)

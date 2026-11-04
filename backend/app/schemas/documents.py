import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.llm.prompts.summary import SummaryLength, SummaryTone


class DocumentError(BaseModel):
    code: str
    message: str


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    original_filename: str
    extension: str
    content_type: str
    size_bytes: int
    status: str = Field(description="queued | processing | ready | failed")
    stage: str | None = Field(description="Current pipeline stage while processing: parse | chunk | embed | analyze")
    insights_status: str = Field(description="pending | ready | failed (chat works even if insights failed)")
    error: DocumentError | None = None
    page_count: int | None
    chunk_count: int
    token_count: int
    version: int
    version_group_id: uuid.UUID
    is_latest: bool
    attempts: int
    processing_ms: int | None
    created_at: datetime
    updated_at: datetime
    processed_at: datetime | None

    @classmethod
    def from_model(cls, doc: Any) -> "DocumentOut":
        out = cls.model_validate(doc)
        if doc.error_code:
            out.error = DocumentError(code=doc.error_code, message=doc.error_message or "")
        return out


class UploadResult(BaseModel):
    document: DocumentOut
    duplicate: bool = Field(
        description="True if identical content was already uploaded; the existing document is returned"
    )
    message: str


class BatchUploadResult(BaseModel):
    results: list[UploadResult]
    errors: list[dict[str, str]]


class DocumentList(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


class KeyFactOut(BaseModel):
    label: str
    value: str
    page: int | None


class InsightsOut(BaseModel):
    document_id: uuid.UUID
    insights_status: str
    summary: str | None = None
    document_type: str | None = None
    category: str | None = None
    tags: list[str] = []
    key_points: list[str] = []
    key_facts: list[KeyFactOut] = []
    entities: dict[str, list[str]] = {}
    sentiment: dict[str, Any] = {}
    suggested_questions: list[str] = []
    language: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None


class SummaryRequest(BaseModel):
    length: SummaryLength = "medium"
    tone: SummaryTone = "neutral"
    focus: str = Field(default="", max_length=300, description="Optional focus area, e.g. 'exclusions and deductibles'")


class SummaryOut(BaseModel):
    document_id: uuid.UUID
    length: str
    tone: str
    focus: str
    summary: str
    cached: bool
    model: str


class StageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage: str
    status: str
    duration_ms: int | None
    details: dict[str, Any]
    error_message: str | None


class ProcessingRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    attempt: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    queue_wait_ms: int | None
    error_code: str | None
    error_message: str | None
    stages: list[StageOut]


class DocumentStatusOut(BaseModel):
    """Lightweight status for polling (the WebSocket at /api/v1/events pushes the same changes)."""

    id: uuid.UUID
    status: str
    stage: str | None
    insights_status: str
    error: DocumentError | None
    attempts: int
    chunk_count: int
    updated_at: datetime

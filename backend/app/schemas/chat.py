import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SessionCreate(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=1, max_length=20)
    title: str | None = Field(default=None, max_length=255)


class SessionDocument(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    version: int
    status: str


class SessionOut(BaseModel):
    id: uuid.UUID
    title: str
    documents: list[SessionDocument]
    message_count: int
    created_at: datetime
    updated_at: datetime


class SessionList(BaseModel):
    items: list[SessionOut]
    total: int


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    use_cache: bool = Field(default=True, description="Set false to bypass the answer cache (e.g. for evaluations)")


class Citation(BaseModel):
    number: int = Field(description="The [n] marker used in the answer")
    document_id: uuid.UUID
    document_title: str
    page_number: int | None
    chunk_index: int
    snippet: str
    similarity: float


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    role: str
    content: str
    citations: list[Citation] = []
    follow_up_questions: list[str] = []
    grounded: bool | None = None
    abstained: bool | None = None
    standalone_question: str | None = None
    from_cache: bool = False
    cache_match: dict[str, Any] | None = Field(
        default=None,
        description="For cached answers: the earlier question it matched (exact or similar) and how closely",
    )
    latency_ms: int | None = None
    created_at: datetime


class AskResponse(BaseModel):
    question: MessageOut
    answer: MessageOut


class ChatHistory(BaseModel):
    session: SessionOut
    messages: list[MessageOut]


class ComparisonCreate(BaseModel):
    document_ids: list[uuid.UUID] = Field(min_length=2, max_length=5)
    focus: str = Field(default="", max_length=500, description="Optional, e.g. 'flood coverage and deductibles'")


class ComparisonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    document_ids: list[str]
    focus: str
    result: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    completed_at: datetime | None

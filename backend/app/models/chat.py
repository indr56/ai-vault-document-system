import uuid
from typing import Any

from sqlalchemy import Boolean, Column, ForeignKey, Index, Integer, String, Table, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamps, UUIDPrimaryKey
from app.models.document import Document

chat_session_documents = Table(
    "chat_session_documents",
    Base.metadata,
    Column("session_id", ForeignKey("chat_sessions.id", ondelete="CASCADE"), primary_key=True),
    Column("document_id", ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
)


class ChatSession(UUIDPrimaryKey, Timestamps, Base):
    """A conversation scoped to one or more documents."""

    __tablename__ = "chat_sessions"

    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    documents: Mapped[list[Document]] = relationship(secondary=chat_session_documents, lazy="selectin")
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at"
    )

    __table_args__ = (Index("ix_chat_sessions_owner_updated", "owner_id", "updated_at"),)


class ChatMessage(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "chat_messages"

    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # Assistant-only fields
    standalone_question: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    follow_up_questions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    retrieval: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    grounded: Mapped[bool | None] = mapped_column(Boolean)
    abstained: Mapped[bool | None] = mapped_column(Boolean)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (Index("ix_chat_messages_session_created", "session_id", "created_at"),)

"""Status enums shared by models, schemas and services."""

from enum import StrEnum


class DocumentStatus(StrEnum):
    QUEUED = "queued"  # stored, waiting for a worker
    PROCESSING = "processing"  # a worker is parsing / chunking / embedding
    READY = "ready"  # searchable and chat-able
    FAILED = "failed"  # processing failed permanently (see error_code / error_message)


class ProcessingStage(StrEnum):
    PARSE = "parse"
    CHUNK = "chunk"
    EMBED = "embed"
    ANALYZE = "analyze"


class InsightsStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"  # chat still works; insights can be regenerated


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class ComparisonStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

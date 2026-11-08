import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ApiKeyCreate(BaseModel):
    name: str = Field(default="default", min_length=1, max_length=100, description="A label, e.g. 'ci' or 'laptop'")


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    key_prefix: str = Field(description="First characters of the key, to recognise it")
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full key. Shown only once: only its SHA-256 hash is stored.")


class EventsTicket(BaseModel):
    ticket: str = Field(description="Single-use; pass as ?ticket= when opening the events WebSocket")
    expires_in: int = Field(description="Seconds until the ticket expires if unused")

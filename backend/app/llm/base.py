"""Provider-agnostic interface for chat completions and embeddings.

Services depend on `LLMProvider`, never on a vendor SDK, so swapping OpenAI for Claude (or a
fake provider in tests) is a configuration change.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

Message = dict[str, str]  # {"role": "system" | "user" | "assistant", "content": "..."}


@dataclass
class Usage:
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Completion:
    text: str
    usage: Usage


@dataclass
class StructuredCompletion[T: BaseModel]:
    value: T
    usage: Usage


@dataclass
class Embeddings:
    vectors: list[list[float]]
    usage: Usage


@dataclass
class StreamEvent:
    """Either a text delta, or (at the end) the final usage."""

    delta: str = ""
    usage: Usage | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    chat_model: str
    fast_model: str
    embedding_model: str

    # --- sync (Celery workers) ---
    def embed(self, texts: list[str]) -> Embeddings: ...

    def complete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> StructuredCompletion[T]: ...

    # --- async (API) ---
    async def aembed(self, texts: list[str]) -> Embeddings: ...

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> Completion: ...

    async def acomplete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> StructuredCompletion[T]: ...

    def astream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> AsyncIterator[StreamEvent]: ...

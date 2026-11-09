"""OpenAI implementation of `LLMProvider` (chat completions, structured outputs, embeddings)."""

import time
from collections.abc import AsyncIterator
from typing import TypeVar

import openai
from openai import AsyncOpenAI, OpenAI
from pydantic import BaseModel

from app.core.config import Settings
from app.core.errors import AIServiceError
from app.llm.base import Completion, Embeddings, Message, StreamEvent, StructuredCompletion, Usage

T = TypeVar("T", bound=BaseModel)

# Errors worth retrying at the job level (the SDK already retries a few times internally).
TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def to_service_error(exc: Exception) -> AIServiceError:
    """Translate SDK errors into a clear API error (never leak provider internals to clients)."""
    if isinstance(exc, TRANSIENT_ERRORS):
        return AIServiceError("The AI service is temporarily unavailable. Please retry.")
    if isinstance(exc, openai.AuthenticationError):
        return AIServiceError("The AI provider rejected the API key. Check OPENAI_API_KEY.", code="ai_not_configured")
    return AIServiceError("The AI provider could not process the request.", code="ai_request_failed")


def _sampling(temperature: float, reasoning_effort: str | None) -> dict[str, object]:
    """With reasoning on, the model only accepts the default temperature, so it is left out."""
    return {"reasoning_effort": reasoning_effort} if reasoning_effort else {"temperature": temperature}


def _ms_since(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings):
        api_key = settings.openai_api_key.get_secret_value()
        if not api_key:
            raise AIServiceError(
                "OPENAI_API_KEY is not set. Add it to your .env file (see .env.example).",
                code="ai_not_configured",
            )
        self.chat_model = settings.chat_model
        self.fast_model = settings.fast_model
        self.embedding_model = settings.embedding_model
        self._dimensions = settings.embedding_dimensions
        common = {"api_key": api_key, "timeout": settings.llm_timeout_seconds, "max_retries": 3}
        self._client = OpenAI(**common)  # type: ignore[arg-type]
        self._aclient = AsyncOpenAI(**common)  # type: ignore[arg-type]

    # ------------------------------------------------------------------ embeddings
    def embed(self, texts: list[str]) -> Embeddings:
        start = time.perf_counter()
        response = self._client.embeddings.create(model=self.embedding_model, input=texts, dimensions=self._dimensions)
        return self._to_embeddings(response, start)

    async def aembed(self, texts: list[str]) -> Embeddings:
        start = time.perf_counter()
        try:
            response = await self._aclient.embeddings.create(
                model=self.embedding_model, input=texts, dimensions=self._dimensions
            )
        except openai.OpenAIError as exc:
            raise to_service_error(exc) from exc
        return self._to_embeddings(response, start)

    def _to_embeddings(self, response: openai.types.CreateEmbeddingResponse, start: float) -> Embeddings:
        vectors = [item.embedding for item in sorted(response.data, key=lambda d: d.index)]
        usage = Usage(
            model=self.embedding_model,
            prompt_tokens=response.usage.prompt_tokens,
            latency_ms=_ms_since(start),
        )
        return Embeddings(vectors=vectors, usage=usage)

    # ------------------------------------------------------------------ completions
    def complete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> StructuredCompletion[T]:
        start = time.perf_counter()
        response = self._client.chat.completions.parse(
            model=model or self.chat_model,
            messages=messages,  # type: ignore[arg-type]
            response_format=response_model,
            temperature=temperature,
            max_completion_tokens=max_tokens,
        )
        return self._to_structured(response, response_model, start)

    async def acomplete_structured(
        self,
        messages: list[Message],
        response_model: type[T],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> StructuredCompletion[T]:
        start = time.perf_counter()
        try:
            response = await self._aclient.chat.completions.parse(
                model=model or self.chat_model,
                messages=messages,  # type: ignore[arg-type]
                response_format=response_model,
                temperature=temperature,
                max_completion_tokens=max_tokens,
            )
        except openai.OpenAIError as exc:
            raise to_service_error(exc) from exc
        return self._to_structured(response, response_model, start)

    def _to_structured(self, response, response_model: type[T], start: float) -> StructuredCompletion[T]:  # type: ignore[no-untyped-def]
        message = response.choices[0].message
        if message.refusal or message.parsed is None:
            raise AIServiceError("The model did not return a valid structured response.", code="ai_bad_output")
        return StructuredCompletion(value=message.parsed, usage=self._usage(response, start))

    async def acomplete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        reasoning_effort: str | None = None,
    ) -> Completion:
        start = time.perf_counter()
        try:
            response = await self._aclient.chat.completions.create(  # type: ignore[call-overload]
                model=model or self.chat_model,
                messages=messages,  # type: ignore[arg-type]
                max_completion_tokens=max_tokens,
                **_sampling(temperature, reasoning_effort),
            )
        except openai.OpenAIError as exc:
            raise to_service_error(exc) from exc
        return Completion(text=response.choices[0].message.content or "", usage=self._usage(response, start))

    async def astream(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1200,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        start = time.perf_counter()
        used_model = model or self.chat_model
        try:
            stream = await self._aclient.chat.completions.create(  # type: ignore[call-overload]
                model=used_model,
                messages=messages,  # type: ignore[arg-type]
                max_completion_tokens=max_tokens,
                stream=True,
                **_sampling(temperature, reasoning_effort),
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                used_model = chunk.model or used_model  # the dated model id, as in non-streaming calls
                if chunk.choices and chunk.choices[0].delta.content:
                    yield StreamEvent(delta=chunk.choices[0].delta.content)
                if chunk.usage:
                    yield StreamEvent(
                        usage=Usage(
                            model=used_model,
                            prompt_tokens=chunk.usage.prompt_tokens,
                            completion_tokens=chunk.usage.completion_tokens,
                            latency_ms=_ms_since(start),
                        )
                    )
        except openai.OpenAIError as exc:
            raise to_service_error(exc) from exc

    @staticmethod
    def _usage(response, start: float) -> Usage:  # type: ignore[no-untyped-def]
        return Usage(
            model=response.model,
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
            latency_ms=_ms_since(start),
        )

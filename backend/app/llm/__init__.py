"""LLM access layer. Use `get_provider()`; never import a vendor SDK from services."""

from functools import lru_cache

from app.core.config import get_settings
from app.llm.base import LLMProvider


@lru_cache
def get_provider() -> LLMProvider:
    settings = get_settings()
    if settings.llm_provider == "fake":
        from app.llm.fake_provider import FakeProvider

        return FakeProvider(settings)
    from app.llm.openai_provider import OpenAIProvider

    return OpenAIProvider(settings)

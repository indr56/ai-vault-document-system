"""AI cost tracking: every model call is recorded with tokens, latency and estimated USD cost.

Rules
- A call's usage is written in its OWN short transaction as soon as the call returns, so a later
  rollback (e.g. insights fail after three paid map calls) never erases what was already spent.
  (Chat is the exception: an answer's calls are saved in the same transaction as the messages, and
  a failed or interrupted answer saves them through chat._save_interrupted.)
- A call that raises is recorded too, with success=false (zero tokens: the provider bills nothing
  for a failed request), so "failed calls" in the metrics is real.
- A cache hit is recorded with cached=true, cost 0 and the tokens the cache saved; cached rows are
  excluded from spend totals and from the daily quota, and reported as savings.
- A model missing from the price table is logged once and listed in the metrics ("unpriced_models"),
  instead of silently costing $0.
"""

import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from functools import cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.base import Usage
from app.models import AIUsage

logger = get_logger(__name__)

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def base_model(model: str) -> str:
    """'gpt-5.4-mini-2026-03-17' -> 'gpt-5.4-mini' (the price table uses base names)."""
    return _DATE_SUFFIX.sub("", model)


def is_priced(model: str) -> bool:
    return base_model(model) in get_settings().model_prices


@cache
def _warn_unpriced(model: str) -> None:  # cached: one warning per model per process
    logger.warning("unpriced_model", model=model, hint="add it to MODEL_PRICES; its cost is counted as $0")


def estimate_cost_usd(usage: Usage) -> Decimal:
    model = base_model(usage.model)
    prices = get_settings().model_prices
    if model not in prices:
        _warn_unpriced(model)
    input_price, output_price = prices.get(model, (0.0, 0.0))
    cost = (usage.prompt_tokens * input_price + usage.completion_tokens * output_price) / 1_000_000
    return Decimal(str(round(cost, 6)))


@dataclass
class UsageEntry:
    """One model call (or one avoided call, when cached) waiting to be written."""

    operation: str
    usage: Usage
    prompt_version: str | None = None
    success: bool = True
    cached: bool = False


def build_usage_row(
    usage: Usage,
    *,
    operation: str,
    owner_id: uuid.UUID | None,
    prompt_version: str | None = None,
    document_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    success: bool = True,
    cached: bool = False,
) -> AIUsage:
    return AIUsage(
        owner_id=owner_id,
        operation=operation,
        model=usage.model,
        prompt_version=prompt_version,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        cost_usd=Decimal("0") if cached or not success else estimate_cost_usd(usage),
        latency_ms=usage.latency_ms,
        success=success,
        cached=cached,
        document_id=document_id,
        session_id=session_id,
    )


def row_from_entry(entry: UsageEntry, **ids: Any) -> AIUsage:
    return build_usage_row(
        entry.usage,
        operation=entry.operation,
        prompt_version=entry.prompt_version,
        success=entry.success,
        cached=entry.cached,
        **ids,
    )


def record_usage(usage: Usage, **kwargs: Any) -> None:
    """Sync (workers): write one row in its own transaction. Best effort: never fails the caller."""
    from app.db.session import sync_session

    try:
        with sync_session() as db:
            db.add(build_usage_row(usage, **kwargs))
    except Exception:
        logger.exception("usage_record_failed", operation=kwargs.get("operation"))


def record_entry(entry: UsageEntry, **ids: Any) -> None:
    record_usage(
        entry.usage,
        operation=entry.operation,
        prompt_version=entry.prompt_version,
        success=entry.success,
        cached=entry.cached,
        **ids,
    )


async def arecord_entries(entries: list[UsageEntry], **ids: Any) -> None:
    """Async (API): write rows in their own transaction. Best effort: never fails the caller."""
    from app.db.session import get_async_sessionmaker

    if not entries:
        return
    try:
        async with get_async_sessionmaker()() as db:
            db.add_all([row_from_entry(e, **ids) for e in entries])
            await db.commit()
    except Exception:
        logger.exception("usage_record_failed", operations=[e.operation for e in entries])


def add_entries(db: AsyncSession, entries: list[UsageEntry], **ids: Any) -> None:
    """Same transaction as the caller's other writes (used when saving a chat exchange)."""
    db.add_all([row_from_entry(e, **ids) for e in entries])


@contextmanager
def failed_calls_to(
    sink: Callable[[UsageEntry], None], operation: str, model: str, prompt_version: str | None = None
) -> Iterator[None]:
    """Wrap one model call: if it raises, hand a success=false entry to `sink`, then re-raise."""
    start = time.perf_counter()
    try:
        yield
    except Exception:
        latency = int((time.perf_counter() - start) * 1000)
        sink(UsageEntry(operation, Usage(model=model, latency_ms=latency), prompt_version, success=False))
        raise

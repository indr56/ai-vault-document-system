"""AI cost tracking: every model call is recorded with tokens, latency and estimated USD cost."""

import re
import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.llm.base import Usage
from app.models import AIUsage

_DATE_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def estimate_cost_usd(usage: Usage) -> Decimal:
    base_model = _DATE_SUFFIX.sub("", usage.model)
    input_price, output_price = get_settings().model_prices.get(base_model, (0.0, 0.0))
    cost = (usage.prompt_tokens * input_price + usage.completion_tokens * output_price) / 1_000_000
    return Decimal(str(round(cost, 6)))


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
        cost_usd=Decimal("0") if cached else estimate_cost_usd(usage),
        latency_ms=usage.latency_ms,
        success=success,
        cached=cached,
        document_id=document_id,
        session_id=session_id,
    )


def record_usage(db: Session, usage: Usage, **kwargs) -> None:  # type: ignore[no-untyped-def]
    db.add(build_usage_row(usage, **kwargs))


async def arecord_usage(db: AsyncSession, usage: Usage, **kwargs) -> None:  # type: ignore[no-untyped-def]
    db.add(build_usage_row(usage, **kwargs))

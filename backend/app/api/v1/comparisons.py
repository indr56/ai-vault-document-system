import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, NotReadyError, QueueUnavailableError
from app.core.logging import get_logger
from app.core.rate_limit import rate_limited
from app.db.session import get_db
from app.models import Comparison, User
from app.models.enums import DocumentStatus
from app.schemas.chat import ComparisonCreate, ComparisonOut
from app.services.documents import get_ready_documents
from app.workers import tasks

logger = get_logger(__name__)
router = APIRouter(prefix="/comparisons", tags=["comparisons"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]
ChatUser = Annotated[User, Depends(rate_limited("chat"))]


@router.post(
    "",
    response_model=ComparisonOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Compare 2-5 documents (runs in the background)",
    description="Returns immediately with `status: pending`. Poll GET /comparisons/{id} or listen for "
    "`comparison.completed` on the events WebSocket.",
)
async def create_comparison(body: ComparisonCreate, db: DB, user: ChatUser) -> ComparisonOut:
    ids = list(dict.fromkeys(body.document_ids))
    documents = await get_ready_documents(db, user, ids)
    not_ready = [str(d.id) for d in documents if d.status != DocumentStatus.READY]
    if not_ready:
        raise NotReadyError("All documents must be ready before comparing.", details={"not_ready": not_ready})
    comparison = Comparison(owner_id=user.id, document_ids=[str(i) for i in ids], focus=" ".join(body.focus.split()))
    db.add(comparison)
    await db.commit()
    await db.refresh(comparison)
    try:
        await asyncio.to_thread(tasks.enqueue_comparison, comparison.id)
    except Exception as exc:
        logger.exception("enqueue_failed", comparison_id=str(comparison.id))
        await db.delete(comparison)  # nothing will run it; don't leave a row "pending" forever
        await db.commit()
        raise QueueUnavailableError("The job queue is unavailable right now. Please try again shortly.") from exc
    return ComparisonOut.model_validate(comparison)


@router.get("", response_model=list[ComparisonOut])
async def list_comparisons(db: DB, user: CurrentUser) -> list[ComparisonOut]:
    rows = (
        (
            await db.execute(
                select(Comparison)
                .where(Comparison.owner_id == user.id)
                .order_by(Comparison.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return [ComparisonOut.model_validate(c) for c in rows]


@router.get("/{comparison_id}", response_model=ComparisonOut)
async def get_comparison(comparison_id: uuid.UUID, db: DB, user: CurrentUser) -> ComparisonOut:
    comparison = (
        await db.execute(select(Comparison).where(Comparison.id == comparison_id, Comparison.owner_id == user.id))
    ).scalar_one_or_none()
    if comparison is None:
        raise NotFoundError("Comparison not found.", code="comparison_not_found")
    return ComparisonOut.model_validate(comparison)

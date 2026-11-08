"""Document management (API side): upload & validation, duplicates, versioning, listing,
deletion, reprocessing and customised summaries. Heavy work is delegated to Celery workers."""

import asyncio
import hashlib
import uuid
from pathlib import PurePath

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    NotReadyError,
    PayloadTooLargeError,
    QueueUnavailableError,
    UnsupportedMediaError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.db.session import sync_session
from app.llm import get_provider
from app.llm.prompts import summary as summary_prompt
from app.models import Document, DocumentInsight, DocumentSummary, ProcessingRun, User
from app.models.enums import DocumentStatus, InsightsStatus
from app.services import events
from app.services.insights import prepare_analysis_text
from app.services.parsing import parse_document
from app.services.storage import get_storage
from app.services.usage import UsageEntry, arecord_entries, failed_calls_to
from app.workers import tasks

logger = get_logger(__name__)

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
}
# Cheap content sniffing so a renamed file (e.g. an .exe called report.pdf) is rejected up front.
MAGIC_BYTES = {".pdf": b"%PDF-", ".docx": b"PK\x03\x04"}


# --------------------------------------------------------------------------- validation
def validate_upload(filename: str | None, data: bytes) -> str:
    """Returns the normalised extension or raises a clear, user-facing error."""
    settings = get_settings()
    if not filename:
        raise ValidationFailedError("The uploaded file has no name.", code="missing_filename")
    extension = PurePath(filename).suffix.lower()
    if extension not in settings.allowed_extensions:
        raise UnsupportedMediaError(
            f"Unsupported file type '{extension or 'none'}'.",
            details={"allowed": list(settings.allowed_extensions)},
        )
    if len(data) == 0:
        raise ValidationFailedError("The file is empty.", code="empty_file")
    if len(data) > settings.max_upload_bytes:
        raise PayloadTooLargeError(
            f"The file is larger than the {settings.max_upload_mb} MB limit.",
            details={"max_mb": settings.max_upload_mb, "size_bytes": len(data)},
        )
    magic = MAGIC_BYTES.get(extension)
    if magic and not data.startswith(magic):
        raise UnsupportedMediaError(
            f"The file content does not look like a valid {extension} file.", code="invalid_file_content"
        )
    if extension in (".txt", ".md") and b"\x00" in data[:8192]:
        raise UnsupportedMediaError("The file looks binary, not text.", code="invalid_file_content")
    return extension


def _title_from(filename: str) -> str:
    stem = PurePath(filename).stem.replace("_", " ").replace("-", " ").strip()
    return (stem or filename)[:255]


# --------------------------------------------------------------------------- queries
async def get_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> Document:
    document = (
        await db.execute(select(Document).where(Document.id == document_id, Document.owner_id == user.id))
    ).scalar_one_or_none()
    if document is None:
        raise NotFoundError("Document not found.", code="document_not_found")
    return document


async def get_ready_documents(db: AsyncSession, user: User, document_ids: list[uuid.UUID]) -> list[Document]:
    documents = (
        (await db.execute(select(Document).where(Document.id.in_(document_ids), Document.owner_id == user.id)))
        .scalars()
        .all()
    )
    found = {d.id for d in documents}
    missing = [str(d) for d in document_ids if d not in found]
    if missing:
        raise NotFoundError("Some documents were not found.", code="document_not_found", details={"missing": missing})
    by_id = {d.id: d for d in documents}
    return [by_id[d] for d in document_ids]


def _list_query(
    user: User,
    *,
    status: str | None,
    category: str | None,
    tag: str | None,
    q: str | None,
    include_old_versions: bool,
) -> Select:  # type: ignore[type-arg]
    stmt = select(Document).where(Document.owner_id == user.id)
    if not include_old_versions:
        stmt = stmt.where(Document.is_latest.is_(True))
    if status:
        stmt = stmt.where(Document.status == status)
    if category or tag:
        stmt = stmt.join(DocumentInsight, DocumentInsight.document_id == Document.id)
        if category:
            stmt = stmt.where(DocumentInsight.category == category)
        if tag:
            stmt = stmt.where(DocumentInsight.tags.contains([tag.lower()]))
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(Document.title.ilike(pattern), Document.original_filename.ilike(pattern)))
    return stmt


async def list_documents(
    db: AsyncSession,
    user: User,
    *,
    status: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    include_old_versions: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Document], int]:
    base = _list_query(user, status=status, category=category, tag=tag, q=q, include_old_versions=include_old_versions)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(base.order_by(Document.created_at.desc()).limit(limit).offset(offset))).scalars().all()
    return list(rows), total


async def list_versions(db: AsyncSession, user: User, document_id: uuid.UUID) -> list[Document]:
    document = await get_document(db, user, document_id)
    rows = (
        (
            await db.execute(
                select(Document)
                .where(Document.version_group_id == document.version_group_id, Document.owner_id == user.id)
                .order_by(Document.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def list_processing_runs(db: AsyncSession, user: User, document_id: uuid.UUID) -> list[ProcessingRun]:
    from sqlalchemy.orm import selectinload

    await get_document(db, user, document_id)
    rows = (
        (
            await db.execute(
                select(ProcessingRun)
                .options(selectinload(ProcessingRun.stages))
                .where(ProcessingRun.document_id == document_id)
                .order_by(ProcessingRun.started_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# --------------------------------------------------------------------------- commands
def enqueue_processing(document_id: uuid.UUID) -> None:
    """Hand the document to a worker. If the broker is briefly unavailable the document stays
    `queued` and the periodic `requeue_stale_documents` task picks it up later."""
    try:
        tasks.enqueue_document(document_id)
    except Exception:
        logger.exception("enqueue_failed", document_id=str(document_id))


VERSION_CONFLICT_RETRIES = 2


async def _find_duplicate(db: AsyncSession, owner_id: uuid.UUID, content_hash: str) -> Document | None:
    return (
        await db.execute(
            select(Document)
            .where(Document.owner_id == owner_id, Document.content_hash == content_hash)
            .order_by(Document.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def upload_document(
    db: AsyncSession,
    user: User,
    *,
    filename: str | None,
    data: bytes,
    title: str | None = None,
    replaces_document_id: uuid.UUID | None = None,
) -> tuple[Document, bool]:
    """Validate, store and queue a file. Returns (document, is_duplicate).

    - Duplicate detection: identical content (SHA-256) already in the vault returns the existing
      document instead of storing and paying to process it again.
    - Versioning: pass `replaces_document_id` to upload a new version of an existing document.
    """
    extension = validate_upload(filename, data)
    assert filename is not None
    content_hash = hashlib.sha256(data).hexdigest()

    owner_id = user.id  # plain values: a rollback below expires ORM objects
    duplicate = await _find_duplicate(db, owner_id, content_hash)
    if duplicate is not None:
        return duplicate, True

    count = (
        await db.execute(select(func.count()).where(Document.owner_id == user.id, Document.is_latest.is_(True)))
    ).scalar_one()
    if count >= get_settings().max_documents_per_user:
        raise ConflictError("Document limit reached for this account.", code="document_limit_reached")

    previous = await get_document(db, user, replaces_document_id) if replaces_document_id else None
    previous_group = previous.version_group_id if previous is not None else None
    # A new version keeps the document's title (the file name often changes: policy_final_v2.pdf).
    default_title = previous.title if previous is not None else _title_from(filename)
    document_id = uuid.uuid4()
    storage_key = await asyncio.to_thread(get_storage().save, user.id, document_id, extension, data)
    # The database is the final judge of the two race conditions the checks above can't rule out:
    # - the same file uploaded twice at the same moment -> unique (owner_id, content_hash);
    # - two new versions of one document at the same moment -> unique (version_group_id, version)
    #   and at most one is_latest row per group (partial unique index).
    # On a conflict: same file -> return the row that won; version clash -> recompute and try again.
    for attempt in range(VERSION_CONFLICT_RETRIES + 1):
        version_group_id, version = uuid.uuid4(), 1
        if previous_group is not None:
            latest_version = (
                await db.execute(select(func.max(Document.version)).where(Document.version_group_id == previous_group))
            ).scalar_one()
            version_group_id, version = previous_group, (latest_version or 0) + 1
            await db.execute(
                update(Document).where(Document.version_group_id == version_group_id).values(is_latest=False)
            )
        document = Document(
            id=document_id,
            owner_id=owner_id,
            title=(title or default_title)[:255],
            original_filename=PurePath(filename).name[:255],
            extension=extension,
            content_type=CONTENT_TYPES[extension],
            size_bytes=len(data),
            content_hash=content_hash,
            storage_path=storage_key,
            status=DocumentStatus.QUEUED,
            insights_status=InsightsStatus.PENDING,
            version_group_id=version_group_id,
            version=version,
            is_latest=True,
        )
        db.add(document)
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
            winner = await _find_duplicate(db, owner_id, content_hash)
            if winner is not None or attempt == VERSION_CONFLICT_RETRIES:
                await asyncio.to_thread(get_storage().delete, storage_key)  # our copy of the file is unused
                if winner is not None:
                    return winner, True
                raise ConflictError(
                    "Another version of this document was uploaded at the same moment. Please retry.",
                    code="version_conflict",
                ) from None
            logger.info("version_conflict_retry", document_id=str(document_id), attempt=attempt + 1)
    await db.refresh(document)
    await asyncio.to_thread(enqueue_processing, document.id)
    events.publish(owner_id, "document.queued", {"document_id": document.id})
    logger.info("document_uploaded", document_id=str(document.id), size=len(data), version=version)
    return document, False


async def reprocess_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> Document:
    document = await get_document(db, user, document_id)
    if document.status == DocumentStatus.PROCESSING:
        raise ConflictError("The document is already being processed.", code="already_processing")
    document.status = DocumentStatus.QUEUED
    document.stage = None
    document.error_code = None
    document.error_message = None
    document.insights_status = InsightsStatus.PENDING
    await db.commit()
    await db.refresh(document)
    await asyncio.to_thread(enqueue_processing, document.id)
    return document


async def regenerate_insights(db: AsyncSession, user: User, document_id: uuid.UUID) -> Document:
    document = await get_document(db, user, document_id)
    if document.status != DocumentStatus.READY:
        raise NotReadyError("Insights can be regenerated once the document is ready.")
    previous_status = document.insights_status
    document.insights_status = InsightsStatus.PENDING
    await db.commit()
    try:
        await asyncio.to_thread(tasks.enqueue_insights, document.id)
    except Exception as exc:
        logger.exception("enqueue_failed", document_id=str(document.id), job="regenerate_insights")
        document.insights_status = previous_status  # nothing will run, so don't show "pending" forever
        await db.commit()
        raise QueueUnavailableError("The job queue is unavailable right now. Please try again shortly.") from exc
    await db.refresh(document)
    return document


async def delete_document(db: AsyncSession, user: User, document_id: uuid.UUID) -> None:
    """Deletes one version. If it was the latest, the previous version becomes the latest."""
    document = await get_document(db, user, document_id)
    if document.status == DocumentStatus.PROCESSING:
        # The worker is writing chunks for this row right now; deleting under it would make its next
        # write fail. Processing takes seconds to a few minutes, so ask the caller to retry.
        raise ConflictError(
            "The document is being processed. Delete it once processing finishes.", code="document_processing"
        )
    storage_key, was_latest, group_id = document.storage_path, document.is_latest, document.version_group_id
    await db.delete(document)
    await db.flush()
    if was_latest:
        previous = (
            await db.execute(
                select(Document).where(Document.version_group_id == group_id).order_by(Document.version.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if previous is not None:
            previous.is_latest = True
    await db.commit()
    await asyncio.to_thread(get_storage().delete, storage_key)
    events.publish(user.id, "document.deleted", {"document_id": document_id})


# --------------------------------------------------------------------------- summaries
def _load_summary_text(document_id: uuid.UUID) -> str:
    """Runs in a thread: re-parse the stored file; long documents are condensed (map step)."""
    with sync_session() as db:
        document = db.get(Document, document_id)
        assert document is not None
        parsed = parse_document(get_storage().path(document.storage_path), document.extension)
        return prepare_analysis_text(db, document, parsed)


async def custom_summary(
    db: AsyncSession, user: User, document_id: uuid.UUID, *, length: str, tone: str, focus: str
) -> tuple[DocumentSummary, bool]:
    """Returns (summary, cached). Each (length, tone, focus) variant is generated once and stored."""
    document = await get_document(db, user, document_id)
    if document.status != DocumentStatus.READY:
        raise NotReadyError("The document is not ready yet.", details={"status": document.status})
    focus = " ".join(focus.split())
    existing = await _find_summary(db, document.id, length, tone, focus)
    if existing is not None:
        return existing, True

    text = await asyncio.to_thread(_load_summary_text, document.id)
    provider = get_provider()
    ids = {"owner_id": user.id, "document_id": document.id}
    failed: list[UsageEntry] = []
    try:
        with failed_calls_to(failed.append, "custom_summary", provider.chat_model, summary_prompt.VERSION):
            completion = await provider.acomplete(
                summary_prompt.build_messages(document.title, text, length, tone, focus), max_tokens=900
            )
    finally:
        await arecord_entries(failed, **ids)
    # Own transaction: the call is paid for even if saving the summary below loses a race.
    await arecord_entries([UsageEntry("custom_summary", completion.usage, summary_prompt.VERSION)], **ids)
    summary = DocumentSummary(
        document_id=document.id,
        length=length,
        tone=tone,
        focus=focus,
        content=completion.text.strip(),
        model=completion.usage.model,
    )
    db.add(summary)
    try:
        await db.commit()
    except IntegrityError:
        # Two identical requests at the same moment: the other one saved this variant first
        # (unique constraint uq_document_summaries_variant). Return the stored one instead of a 500.
        await db.rollback()
        stored = await _find_summary(
            db, ids["document_id"], length, tone, focus
        )  # plain id: rollback expired ORM objects
        if stored is None:  # pragma: no cover - the conflicting row was deleted in between
            raise
        return stored, True
    return summary, False


async def _find_summary(
    db: AsyncSession, document_id: uuid.UUID, length: str, tone: str, focus: str
) -> DocumentSummary | None:
    return (
        await db.execute(
            select(DocumentSummary).where(
                DocumentSummary.document_id == document_id,
                DocumentSummary.length == length,
                DocumentSummary.tone == tone,
                DocumentSummary.focus == focus,
            )
        )
    ).scalar_one_or_none()

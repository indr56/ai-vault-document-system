import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError, PayloadTooLargeError, ValidationFailedError, error_responses
from app.core.rate_limit import rate_limited
from app.db.session import get_db
from app.models import User
from app.models.enums import DocumentStatus, InsightsStatus
from app.schemas.documents import (
    BatchUploadResult,
    DocumentList,
    DocumentOut,
    DocumentStatusOut,
    InsightsOut,
    ProcessingRunOut,
    SummaryOut,
    SummaryRequest,
    UploadResult,
)
from app.services import documents as service
from app.services.storage import get_storage

router = APIRouter(prefix="/documents", tags=["documents"])

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(rate_limited("default"))]
UploadUser = Annotated[User, Depends(rate_limited("upload"))]


async def _read_limited(file: UploadFile) -> bytes:
    """Read at most limit+1 bytes so an oversized upload is rejected without buffering all of it."""
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise PayloadTooLargeError(
            f"'{file.filename}' is larger than the {get_settings().max_upload_mb} MB limit.",
            details={"max_mb": get_settings().max_upload_mb},
        )
    return data


def _upload_result(outcome: service.UploadOutcome) -> UploadResult:
    if outcome.reprocessing:
        message = "This file was uploaded before but failed to process; processing it again."
    elif outcome.duplicate and outcome.latest_title is not None:
        message = (
            f'Identical to version {outcome.document.version} of "{outcome.latest_title}" (an older version) '
            "already in your vault; returning it."
        )
    elif outcome.duplicate:
        message = "Identical file already in your vault; returning the existing document."
    else:
        message = "Uploaded. Processing has started."
    return UploadResult(document=DocumentOut.from_model(outcome.document), duplicate=outcome.duplicate, message=message)


@router.post(
    "",
    responses=error_responses(409, 413, 415),
    response_model=UploadResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a document (PDF, DOCX, TXT, MD)",
    description="Stores the file and queues it for processing. Returns immediately (202); follow progress "
    "with GET /documents/{id}/status or the events WebSocket. Pass `replaces_document_id` to upload a new "
    "version of an existing document (it keeps that document's title unless `title` is given).",
)
async def upload_document(
    db: DB,
    user: UploadUser,
    response: Response,
    file: UploadFile = File(...),
    title: str | None = Form(default=None, max_length=255),
    replaces_document_id: uuid.UUID | None = Form(default=None),
) -> UploadResult:
    data = await _read_limited(file)
    outcome = await service.upload_document(
        db, user, filename=file.filename, data=data, title=title, replaces_document_id=replaces_document_id
    )
    if outcome.duplicate and not outcome.reprocessing:
        response.status_code = status.HTTP_200_OK  # nothing new was queued
    return _upload_result(outcome)


@router.post(
    "/batch",
    responses=error_responses(413),
    response_model=BatchUploadResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload several documents at once",
    description="Each file is validated independently: valid files are queued even if others fail.",
)
async def upload_batch(db: DB, user: UploadUser, files: list[UploadFile] = File(...)) -> BatchUploadResult:
    max_files = get_settings().max_batch_files
    if len(files) > max_files:
        raise ValidationFailedError(f"At most {max_files} files per batch.", code="too_many_files")
    results: list[UploadResult] = []
    errors: list[dict[str, str]] = []
    for file in files:
        try:
            data = await _read_limited(file)
            outcome = await service.upload_document(db, user, filename=file.filename, data=data)
            results.append(_upload_result(outcome))
        except AppError as exc:
            await db.rollback()
            # The rollback expires every loaded object, the user included; reload it here, because
            # reading user.id lazily on the next file would fail on the async session.
            await db.refresh(user)
            errors.append({"filename": file.filename or "", "code": exc.code, "message": exc.message})
    return BatchUploadResult(results=results, errors=errors)


@router.get("", response_model=DocumentList, summary="List documents (latest versions by default)")
async def list_documents(
    db: DB,
    user: CurrentUser,
    status_filter: Annotated[DocumentStatus | None, Query(alias="status")] = None,
    category: str | None = None,
    tag: str | None = None,
    q: Annotated[str | None, Query(max_length=200, description="Search in title / filename")] = None,
    include_old_versions: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    items, total = await service.list_documents(
        db,
        user,
        status=status_filter,
        category=category,
        tag=tag,
        q=q,
        include_old_versions=include_old_versions,
        limit=limit,
        offset=offset,
    )
    return DocumentList(items=[DocumentOut.from_model(d) for d in items], total=total, limit=limit, offset=offset)


@router.get("/{document_id}", response_model=DocumentOut, summary="One document: status, stage, warnings, error")
async def get_document(document_id: uuid.UUID, db: DB, user: CurrentUser) -> DocumentOut:
    return DocumentOut.from_model(await service.get_document(db, user, document_id))


@router.get(
    "/{document_id}/file",
    response_class=FileResponse,
    summary="Download the original file",
    description="The exact bytes that were uploaded, with the original file name.",
)
async def download_document(document_id: uuid.UUID, db: DB, user: CurrentUser) -> FileResponse:
    doc = await service.get_document(db, user, document_id)
    path = get_storage().path(doc.storage_path)
    if not path.is_file():
        raise NotFoundError("The stored file is missing.", code="file_missing")
    return FileResponse(path, media_type=doc.content_type, filename=doc.original_filename)


@router.get("/{document_id}/status", response_model=DocumentStatusOut, summary="Lightweight status for polling")
async def get_status(document_id: uuid.UUID, db: DB, user: CurrentUser) -> DocumentStatusOut:
    doc = await service.get_document(db, user, document_id)
    out = DocumentOut.from_model(doc)
    return DocumentStatusOut(
        id=doc.id,
        status=doc.status,
        stage=doc.stage,
        insights_status=doc.insights_status,
        error=out.error,
        attempts=doc.attempts,
        chunk_count=doc.chunk_count,
        updated_at=doc.updated_at,
    )


@router.get("/{document_id}/insights", response_model=InsightsOut, summary="AI summary, tags, key facts, entities")
async def get_insights(document_id: uuid.UUID, db: DB, user: CurrentUser) -> InsightsOut:
    doc = await service.get_document(db, user, document_id)
    await db.refresh(doc, ["insight"])
    insight = doc.insight
    if insight is None or doc.insights_status != InsightsStatus.READY:
        return InsightsOut(document_id=doc.id, insights_status=doc.insights_status)
    return InsightsOut(
        document_id=doc.id,
        insights_status=doc.insights_status,
        summary=insight.summary,
        document_type=insight.document_type,
        category=insight.category,
        tags=insight.tags,
        key_points=insight.key_points,
        key_facts=insight.key_facts,  # type: ignore[arg-type]
        entities=insight.entities,
        sentiment=insight.sentiment,
        suggested_questions=insight.suggested_questions,
        language=insight.language,
        model=insight.model,
        prompt_version=insight.prompt_version,
        generated_at=insight.updated_at,
    )


@router.post(
    "/{document_id}/summaries",
    responses=error_responses(409, 503),
    response_model=SummaryOut,
    summary="Customised summary (length, tone, focus)",
    description="Each combination is generated once and then served from storage.",
)
async def create_summary(document_id: uuid.UUID, body: SummaryRequest, db: DB, user: UploadUser) -> SummaryOut:
    summary, cached = await service.custom_summary(
        db, user, document_id, length=body.length, tone=body.tone, focus=body.focus
    )
    return SummaryOut(
        document_id=document_id,
        length=summary.length,
        tone=summary.tone,
        focus=summary.focus,
        summary=summary.content,
        cached=cached,
        model=summary.model,
    )


@router.get("/{document_id}/versions", response_model=list[DocumentOut], summary="All versions, newest first")
async def list_versions(document_id: uuid.UUID, db: DB, user: CurrentUser) -> list[DocumentOut]:
    return [DocumentOut.from_model(d) for d in await service.list_versions(db, user, document_id)]


@router.get(
    "/{document_id}/processing-runs",
    response_model=list[ProcessingRunOut],
    summary="Processing attempts with per-stage timings",
)
async def list_runs(document_id: uuid.UUID, db: DB, user: CurrentUser) -> list[ProcessingRunOut]:
    runs = await service.list_processing_runs(db, user, document_id)
    return [ProcessingRunOut.model_validate(r) for r in runs]


@router.post(
    "/{document_id}/reprocess",
    responses=error_responses(409),
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Retry processing (e.g. after a failure)",
)
async def reprocess(document_id: uuid.UUID, db: DB, user: UploadUser) -> DocumentOut:
    return DocumentOut.from_model(await service.reprocess_document(db, user, document_id))


@router.post(
    "/{document_id}/insights/regenerate",
    responses=error_responses(409, 503),
    response_model=DocumentOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run only the AI analysis step",
)
async def regenerate_insights(document_id: uuid.UUID, db: DB, user: UploadUser) -> DocumentOut:
    return DocumentOut.from_model(await service.regenerate_insights(db, user, document_id))


@router.delete(
    "/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a document version",
    responses=error_responses(409),
)
async def delete_document(document_id: uuid.UUID, db: DB, user: CurrentUser) -> Response:
    await service.delete_document(db, user, document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

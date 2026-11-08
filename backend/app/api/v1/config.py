"""Client-facing configuration, so clients (the UI) don't hard-code server rules.

Adding a file type or changing the size limit is then a backend-only change: the UI's file picker
and upload hints read it from here.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from app.core.config import get_settings
from app.core.rate_limit import rate_limited
from app.models import User

router = APIRouter(tags=["config"])
CurrentUser = Annotated[User, Depends(rate_limited("default"))]


@router.get("/config", summary="Upload rules: allowed file types, size and batch limits")
async def client_config(user: CurrentUser) -> dict[str, Any]:
    settings = get_settings()
    return {
        "upload": {
            "allowed_extensions": list(settings.allowed_extensions),
            "max_upload_mb": settings.max_upload_mb,
            "max_batch_files": settings.max_batch_files,
            "max_documents_per_user": settings.max_documents_per_user,
        }
    }

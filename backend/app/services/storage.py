"""File storage behind a small interface. Local disk today; an S3 implementation would only need
these three methods (see README: "Scaling")."""

import uuid
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class Storage(Protocol):
    def save(self, owner_id: uuid.UUID, document_id: uuid.UUID, extension: str, data: bytes) -> str: ...

    def path(self, key: str) -> Path: ...

    def delete(self, key: str) -> None: ...


class LocalStorage:
    def __init__(self, root: Path):
        self.root = root

    def save(self, owner_id: uuid.UUID, document_id: uuid.UUID, extension: str, data: bytes) -> str:
        key = f"{owner_id}/{document_id}{extension}"
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)  # atomic: workers never see a half-written file
        return key

    def path(self, key: str) -> Path:
        resolved = (self.root / key).resolve()
        if not resolved.is_relative_to(self.root.resolve()):  # defensive: no path traversal
            raise ValueError("Invalid storage key")
        return resolved

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def get_storage() -> Storage:
    return LocalStorage(get_settings().storage_dir)

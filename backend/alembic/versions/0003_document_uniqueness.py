"""enforce duplicate detection and versioning in the database

- one row per (owner, file content): two identical uploads at the same moment can't both be saved
- unique version numbers per version group, and at most one is_latest row per group

Revision ID: 0003_document_uniqueness
Revises: 0002_document_warnings
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_document_uniqueness"
down_revision: str | None = "0002_document_warnings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_documents_owner_hash", table_name="documents")
    op.drop_index("ix_documents_version_group", table_name="documents")
    op.create_index("uq_documents_owner_hash", "documents", ["owner_id", "content_hash"], unique=True)
    op.create_index("uq_documents_version_group_version", "documents", ["version_group_id", "version"], unique=True)
    op.create_index(
        "uq_documents_one_latest_per_group",
        "documents",
        ["version_group_id"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )


def downgrade() -> None:
    op.drop_index("uq_documents_one_latest_per_group", table_name="documents")
    op.drop_index("uq_documents_version_group_version", table_name="documents")
    op.drop_index("uq_documents_owner_hash", table_name="documents")
    op.create_index("ix_documents_version_group", "documents", ["version_group_id", "version"], unique=False)
    op.create_index("ix_documents_owner_hash", "documents", ["owner_id", "content_hash"], unique=False)

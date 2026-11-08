"""index request_metrics by owner (GET /metrics/api is filtered per user)

Revision ID: 0004_request_metrics_owner
Revises: 0003_document_uniqueness
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_request_metrics_owner"
down_revision: str | None = "0003_document_uniqueness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_request_metrics_owner_created", "request_metrics", ["owner_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_request_metrics_owner_created", table_name="request_metrics")

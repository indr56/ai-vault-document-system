"""bring the vector extension up to the version the (now pinned) image ships

The database image is pinned to pgvector 0.8.7. A database created with an older image keeps its
older extension version until updated; retrieval needs 0.8+ (hnsw.iterative_scan).

Revision ID: 0005_update_pgvector
Revises: 0004_request_metrics_owner
Create Date: 2026-11-09
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_update_pgvector"
down_revision: str | None = "0004_request_metrics_owner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER EXTENSION vector UPDATE")  # no-op when already at the installed version


def downgrade() -> None:
    pass  # extension downgrades aren't supported by pgvector; nothing to undo

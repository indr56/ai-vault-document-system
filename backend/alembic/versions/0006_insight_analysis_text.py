"""keep the condensed text of long documents with their insights (reused by custom summaries)

Revision ID: 0006_insight_analysis_text
Revises: 0005_update_pgvector
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_insight_analysis_text"
down_revision: str | None = "0005_update_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("document_insights", sa.Column("analysis_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("document_insights", "analysis_text")

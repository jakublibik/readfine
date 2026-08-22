"""record provider and model on article_ai_jobs

Which model produced a score, summary or context was not stored anywhere, so an
ai_score could not be traced back to the model that wrote it. ai_usage_logs
already carries both columns; this brings the per-article jobs in line.

Nullable with no backfill: rows written before this migration have no
recoverable answer, and NULL says exactly that instead of guessing.

Revision ID: 0094
Revises: 0093
Create Date: 2026-08-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("article_ai_jobs", sa.Column("provider", sa.String(length=50), nullable=True))
    op.add_column("article_ai_jobs", sa.Column("model", sa.String(length=100), nullable=True))


def downgrade() -> None:
    op.drop_column("article_ai_jobs", "model")
    op.drop_column("article_ai_jobs", "provider")

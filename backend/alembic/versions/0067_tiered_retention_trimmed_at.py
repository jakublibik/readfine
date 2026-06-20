"""tiered retention: articles.trimmed_at + age-based purge defaults

Revision ID: 0067
Revises: 0066
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Marker for retention-trimmed articles (body stripped to profile snippet,
    # hidden from listings/search/counts). NULL = not trimmed.
    op.add_column("articles", sa.Column("trimmed_at", sa.DateTime(timezone=True), nullable=True))

    # Switch global retention from count-based to age-based.
    # Row id=1 is guaranteed to exist (seeded in 0001_initial_schema).
    op.execute(
        "UPDATE app_settings "
        "SET default_purge_after_days = 60, default_purge_keep_count = NULL "
        "WHERE id = 1"
    )


def downgrade() -> None:
    # Original per-row values are not reconstructable; restore prior defaults.
    op.execute(
        "UPDATE app_settings "
        "SET default_purge_after_days = 90, default_purge_keep_count = 500 "
        "WHERE id = 1"
    )
    op.drop_column("articles", "trimmed_at")

"""add feeds.retry_after_until

Stores the absolute time until which the scheduler must skip a feed, so an HTTP
429 (Too Many Requests) with a ``Retry-After`` header backs the feed off instead
of disabling it. Nullable: feeds without an active rate-limit have no value.

Revision ID: 0073
Revises: 0072
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column("retry_after_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("feeds", "retry_after_until")

"""add readable_failed_at to articles

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "articles",
        sa.Column("readable_failed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("articles", "readable_failed_at")

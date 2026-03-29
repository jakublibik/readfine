"""add readable_error to articles

Revision ID: 0017
Revises: 0016
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("readable_error", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("articles", "readable_error")

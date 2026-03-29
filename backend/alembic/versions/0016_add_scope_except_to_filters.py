"""add scope_except to filters

Revision ID: 0016
Revises: 0015
Create Date: 2026-03-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("filters", sa.Column("scope_except", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("filters", "scope_except")

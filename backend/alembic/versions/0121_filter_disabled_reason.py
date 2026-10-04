"""filters.disabled_reason: why the app switched a filter off by itself

A filter whose regex runs into the match timeout is turned off on the spot, since
every further article would cost the whole process another second. The column
says so in the filter list; saving the filter clears it.

Revision ID: 0121
Revises: 0120
Create Date: 2026-10-04
"""
import sqlalchemy as sa
from alembic import op

revision = "0121"
down_revision = "0120"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("filters", sa.Column("disabled_reason", sa.String(30), nullable=True))


def downgrade() -> None:
    op.drop_column("filters", "disabled_reason")

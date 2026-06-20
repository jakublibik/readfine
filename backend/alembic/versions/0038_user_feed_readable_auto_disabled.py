"""Add readable_auto_disabled to user_feeds

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-09
"""
import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_feeds",
        sa.Column("readable_auto_disabled", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("user_feeds", "readable_auto_disabled")

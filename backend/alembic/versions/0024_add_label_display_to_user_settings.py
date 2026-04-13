"""Add label_display to user_settings

Revision ID: 0024
Revises: 0023
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("label_display", sa.String(20), nullable=False, server_default="indicator"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "label_display")

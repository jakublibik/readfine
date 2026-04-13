"""Drop unused left_panel_pinned from user_settings

Revision ID: 0026
Revises: 0025
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user_settings", "left_panel_pinned")


def downgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("left_panel_pinned", sa.Boolean(), nullable=False, server_default="true"),
    )

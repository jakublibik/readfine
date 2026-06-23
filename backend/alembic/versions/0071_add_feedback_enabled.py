"""add app_settings.feedback_enabled

Adds the global toggle for the in-app "Send feedback / report bug" feature.
Default false: the feedback menu item is hidden until an admin enables it (and
SMTP is configured), so instances that don't want it stay free of UI noise.

Revision ID: 0071
Revises: 0070
Create Date: 2026-06-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("feedback_enabled", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "feedback_enabled")

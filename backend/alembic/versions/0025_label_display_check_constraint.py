"""Add CHECK constraint for user_settings.label_display

Revision ID: 0025
Revises: 0024
Create Date: 2026-04-13
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE user_settings ADD CONSTRAINT ck_user_settings_label_display "
        "CHECK (label_display IN ('none', 'indicator', 'dots'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_settings DROP CONSTRAINT ck_user_settings_label_display")

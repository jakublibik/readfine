"""Add singleton constraint to app_settings

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-24
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app_settings ADD CONSTRAINT ck_app_settings_singleton "
        "CHECK (id = 1)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app_settings DROP CONSTRAINT ck_app_settings_singleton")

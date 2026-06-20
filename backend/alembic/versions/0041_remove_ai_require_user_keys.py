"""Remove ai_require_user_keys from app_settings

Revision ID: 0041
Revises: 0040
Create Date: 2026-05-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("app_settings", "ai_require_user_keys")


def downgrade() -> None:
    op.add_column("app_settings", sa.Column("ai_require_user_keys", sa.Boolean(), nullable=False, server_default="false"))

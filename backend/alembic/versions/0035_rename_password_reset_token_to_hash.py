"""Rename users.password_reset_token to password_reset_token_hash

Revision ID: 0035
Revises: 0034
Create Date: 2026-04-30
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "password_reset_token", new_column_name="password_reset_token_hash")


def downgrade() -> None:
    op.alter_column("users", "password_reset_token_hash", new_column_name="password_reset_token")

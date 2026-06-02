"""add pending email change fields to users

Revision ID: 0066
Revises: 0065
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("pending_email", sa.String(255), nullable=True))
    op.add_column("users", sa.Column(
        "pending_email_token_hash", sa.String(64), nullable=True, unique=True,
    ))
    op.add_column("users", sa.Column(
        "pending_email_expires_at", sa.DateTime(timezone=True), nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("users", "pending_email_expires_at")
    op.drop_column("users", "pending_email_token_hash")
    op.drop_column("users", "pending_email")

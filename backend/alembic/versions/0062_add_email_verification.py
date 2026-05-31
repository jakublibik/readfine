"""add email verification fields to users

Revision ID: 0062
Revises: 0061
Create Date: 2026-05-31
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "email_verified",
        sa.Boolean(),
        nullable=False,
        server_default=text("TRUE"),
    ))
    op.add_column("users", sa.Column(
        "email_verification_token_hash",
        sa.String(64),
        nullable=True,
        unique=True,
    ))
    op.add_column("users", sa.Column(
        "email_verification_expires_at",
        sa.DateTime(timezone=True),
        nullable=True,
    ))


def downgrade() -> None:
    op.drop_column("users", "email_verification_expires_at")
    op.drop_column("users", "email_verification_token_hash")
    op.drop_column("users", "email_verified")

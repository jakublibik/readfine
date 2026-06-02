"""add session_token_version to users

Revision ID: 0065
Revises: 0064
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column(
        "session_token_version",
        sa.Integer(),
        nullable=False,
        server_default=text("0"),
    ))


def downgrade() -> None:
    op.drop_column("users", "session_token_version")

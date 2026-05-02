"""Add fetch_error_count to feeds and extend status with 'disabled'

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column("fetch_error_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("ck_feeds_status", "feeds", type_="check")
    op.create_check_constraint(
        "ck_feeds_status",
        "feeds",
        "status IN ('active', 'error', 'paused', 'disabled')",
    )


def downgrade() -> None:
    op.drop_column("feeds", "fetch_error_count")
    op.drop_constraint("ck_feeds_status", "feeds", type_="check")
    op.create_check_constraint(
        "ck_feeds_status",
        "feeds",
        "status IN ('active', 'error', 'paused')",
    )

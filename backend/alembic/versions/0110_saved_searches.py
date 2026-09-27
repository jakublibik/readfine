"""saved searches

A search the reader named and kept; each one is listed in the sidebar.
The parameters are a JSONB object with the search's query-string keys. Names are
unique per user regardless of case.

Revision ID: 0110
Revises: 0109
Create Date: 2026-09-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "saved_searches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "uq_saved_searches_user_name", "saved_searches",
        ["user_id", sa.text("lower(name)")], unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_saved_searches_user_name", table_name="saved_searches")
    op.drop_table("saved_searches")

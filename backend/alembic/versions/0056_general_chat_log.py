"""add general_chat_log table

Revision ID: 0056
Revises: 0055
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "general_chat_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_general_chat_log_user_id", "general_chat_log", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_general_chat_log_user_id", "general_chat_log")
    op.drop_table("general_chat_log")

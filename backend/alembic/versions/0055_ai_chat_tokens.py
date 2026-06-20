"""add token columns to article_ai_chats

Revision ID: 0055
Revises: 0054
Create Date: 2026-05-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "article_ai_chats",
        sa.Column("total_input_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "article_ai_chats",
        sa.Column("total_output_tokens", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("article_ai_chats", "total_output_tokens")
    op.drop_column("article_ai_chats", "total_input_tokens")

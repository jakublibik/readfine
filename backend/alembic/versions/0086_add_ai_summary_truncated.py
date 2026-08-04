"""flag AI summaries that hit the model's output-token cap

A summary that stopped on max_tokens ends mid-sentence but was stored as a normal
success, so the reader had no way to tell a complete summary from a cut-off one.
This column carries that signal next to the text instead of marking it inside the
summary, which is also served over the API.

Existing rows default to false: they may well be truncated, but there is no way to
tell after the fact, and re-flagging them would mean re-running the summaries.

The column is NOT NULL on a populated table, so it lands with a server_default
that is dropped again right after.

Revision ID: 0086
Revises: 0085
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("ai_summary_truncated", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.alter_column("user_article_states", "ai_summary_truncated", server_default=None)


def downgrade() -> None:
    op.drop_column("user_article_states", "ai_summary_truncated")

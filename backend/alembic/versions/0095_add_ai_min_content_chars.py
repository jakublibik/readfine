"""per-user minimum article length for summaries

The threshold used to be a constant (1500). It is a matter of taste and token
spend, so it becomes a setting. Existing rows are backfilled with the new
default of 1700 rather than kept at the old 1500: nobody on the hosted instance
has AI configured, and 200 characters is a small enough move for self-hosters.

Revision ID: 0095
Revises: 0094
Create Date: 2026-08-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("ai_min_content_chars", sa.Integer(), nullable=False, server_default="1700"),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "ai_min_content_chars")

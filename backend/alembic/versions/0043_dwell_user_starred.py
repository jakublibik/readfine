"""user_starred, dwell_seconds, unstar_dwell_seconds in user_article_states

Revision ID: 0043
Revises: 0042
Create Date: 2026-05-12
"""
import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_article_states", sa.Column("user_starred", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_article_states", sa.Column("dwell_seconds", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("user_article_states", sa.Column("unstar_dwell_seconds", sa.Integer(), nullable=True))

    # Backfill: existing manually starred articles count as user_starred
    op.execute("UPDATE user_article_states SET user_starred = true WHERE is_starred = true")


def downgrade() -> None:
    op.drop_column("user_article_states", "unstar_dwell_seconds")
    op.drop_column("user_article_states", "dwell_seconds")
    op.drop_column("user_article_states", "user_starred")

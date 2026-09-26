"""index the articles whose relevance filters wait for the AI score

process_relevance_fallback looks for relevance_filters_pending rows every ten
minutes. Without an index that is a sequential scan of user_article_states, the
table lexical scoring makes grow by a row per article and subscriber with terms,
to find the handful of rows that are waiting at any moment. Partial, like
ix_uas_ai_filters_pending (0044), so it only ever holds the waiting ones.

Revision ID: 0107
Revises: 0106
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_uas_relevance_filters_pending",
        "user_article_states",
        ["user_id"],
        postgresql_where=sa.text("relevance_filters_pending"),
    )


def downgrade() -> None:
    op.drop_index("ix_uas_relevance_filters_pending", table_name="user_article_states")

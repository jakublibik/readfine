"""index the last-AI-error article FK so bulk article deletes stay cheap

0088 added user_settings.last_ai_error_article_id with ON DELETE SET NULL but no
index, which made it the only foreign key pointing at articles without one (the
four others -- user_article_states, article_labels, article_ai_jobs and
article_ai_chats -- all have one). PostgreSQL does not index a referencing column
automatically, and it runs the referential action per deleted row, so every bulk
DELETE on articles (retention purge, unsubscribe, admin feed removal) would scan
user_settings once per article.

Partial, because the column is NULL for every user who has no pending AI error,
which is nearly all of them at any moment. A lookup by value implies IS NOT NULL,
so the planner can still use it for the referential check.

Separate from 0088 rather than folded into it: 0088 was already committed, and one
extra migration is cheaper than rewriting history for a schema that is identical
either way.

Revision ID: 0089
Revises: 0088
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_user_settings_last_ai_error_article",
        "user_settings",
        ["last_ai_error_article_id"],
        postgresql_where=sa.text("last_ai_error_article_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_user_settings_last_ai_error_article", table_name="user_settings")

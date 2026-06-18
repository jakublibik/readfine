"""index for the default article ordering (coalesce(published_at, fetched_at))

The article list and nav order by coalesce(published_at, fetched_at) DESC across
all subscribed feeds. No existing index served that expression, so the default
"All" / unread views did a full seq scan of articles + top-N sort on every load
(O(total articles)). This partial expression index lets the planner return the
top N by index scan (O(limit)). Partial on trimmed_at IS NULL because trimmed
stubs are never listed. Works for ascending ("oldest") order too via backward
index scan.

Revision ID: 0069
Revises: 0068
Create Date: 2026-06-18
"""
from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_articles_sort_ts ON articles "
        "(coalesce(published_at, fetched_at) DESC, id DESC) "
        "WHERE trimmed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_sort_ts")

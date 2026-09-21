"""cover the sidebar's badge counts so they stop reading the whole articles table

Every badge in the sidebar is its own aggregate over the reader's articles, and four
of them (the nav total and unread, and the same pair per folder) join articles to
user_feeds with nothing but ``trimmed_at IS NULL`` to narrow it. No index fit that
shape, so each one sequentially scanned all of articles, every user's included. The
cost therefore grew with the size of the instance rather than with the size of the
reader's own list, and the sidebar is redrawn on every read, star and archive.

The three columns are everything those queries touch: feed_id is the join to
user_feeds, and story_id with id are what ``story_service.row_count`` counts distinct
over. With the partial predicate matching the filter they all apply, the planner gets
an index-only scan and never reaches the heap, which is where the weight was: articles
is wide, so a scan of 31k rows moves 35 MB.

Measured on a 31k-article database: the sidebar's SQL fell from 328 ms to 106 ms, and
the worst single query, the per-folder unread count, from 51 ms to 17 ms. The index
costs 1.2 MB there. The per-feed counters ride along on the same leading column.

Built with the app down, like every migration here (docker-compose runs alembic before
uvicorn), so a plain build is fine and CONCURRENTLY would buy nothing. It adds nothing
worth mentioning to the upgrade: 50 ms for 31k articles, so a fraction of a second at
production size, against the forty seconds 0098's table rewrite already costs there.

Kept out of the model for the same reason as ix_articles_sort_ts: partial indexes live
in the migrations in this project.

Revision ID: 0102
Revises: 0101
Create Date: 2026-09-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_articles_counts",
        "articles",
        ["feed_id", "story_id", "id"],
        postgresql_where=sa.text("trimmed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_articles_counts", table_name="articles")

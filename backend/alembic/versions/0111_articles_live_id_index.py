"""let the sidebar's label and state counts start from the reader's own rows

The badges over Labels, Starred, Archived and Saved count the reader's article_labels
or user_article_states rows, joined to articles only for ``trimmed_at IS NULL`` and
the story_id that ``story_service.row_count`` counts over. No index answered that
join by id without a heap fetch, so the planner preferred to hash the whole of
ix_articles_counts, every user's articles included, and the cost grew with the
instance instead of with the reader.

With id and story_id in one partial index matching the filter, each of the reader's
rows is a single index-only probe. Measured on 168k articles, 5.7k of them labelled by
the reader: the labelled count fell from 31 ms to 5 ms, the unread labelled count
from 21 ms to 5.5 ms, the starred/archived/saved counts from 15 ms to 8 ms, and the
sidebar's SQL from 93 ms to 56 ms. The same queries serve the label badges redrawn
with a label view.

Built with the app down like every migration here, so a plain build is fine. Kept out
of the model like the other partial indexes on articles (see 0069, 0102).

Revision ID: 0111
Revises: 0110
Create Date: 2026-09-27
"""
from alembic import op

revision = "0111"
down_revision = "0110"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_articles_live_id ON articles (id) INCLUDE (story_id) "
        "WHERE trimmed_at IS NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_live_id")

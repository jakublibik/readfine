"""cross-source story grouping: title_norm, story_id, suppressed_at

Groups near-duplicate coverage of the same story across feeds. The match is lexical
(pg_trgm over a normalised title), limited to a 72 h window, and the threshold (0.30)
was measured on a production export by scripts/survey_dedup.py rather than guessed.

title_norm is a generated column on purpose. Articles are inserted from three places
(fetcher.rss, fetcher.scrape, services.saved_article_service); computing the value in
Python would eventually be forgotten in one of them and nothing would catch it.

story_id holds the id of the oldest member of a group, so a group needs no table of
its own and merging two groups is a single UPDATE. NULL = the article stands alone.

suppressed_at on user_article_states marks an is_read written by the machine rather
than by the user. Without it the URL dedup and the filter mark_read action would count
as "the user has seen this story" and would suppress coverage nobody ever looked at.

This migration does schema only. Grouping the articles already in the table is
scripts/backfill_stories.py, deliberately not run from here: measured against a
production-sized table (135k articles, 81k of them inside the 30-day window) the pair
scan takes between a quarter and half an hour, and everything in a migration is
downtime, because the container runs `alembic upgrade head` before it serves. The
schema part below is about forty seconds of that, nearly all of it the table rewrite
that the generated column forces, and it cannot be avoided. The backfill can: run it
afterwards, with the app up.

Revision ID: 0098
Revises: 0097
Create Date: 2026-09-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # immutable_unaccent comes from 0001; lower() on top of it is what pg_trgm would
    # do internally anyway, but storing it keeps the index and the runtime comparison
    # reading the exact same string.
    op.execute("""
        ALTER TABLE articles ADD COLUMN title_norm TEXT
        GENERATED ALWAYS AS (immutable_unaccent(lower(title))) STORED
    """)
    # No foreign key on story_id on purpose. It points at the oldest member, and with
    # ON DELETE SET NULL the retention purge taking that one article out would dissolve
    # the whole group; with RESTRICT it would block the purge outright. A dangling value
    # costs nothing here: it is a label, the id sequence never hands the number out
    # again, and a later merge still resolves to the same minimum. Anything reading a
    # group must therefore select members by story_id and never assume the article whose
    # id it is still exists.
    op.add_column("articles", sa.Column("story_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "user_article_states",
        sa.Column("suppressed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.execute(
        "CREATE INDEX ix_articles_title_trgm ON articles USING gin (title_norm gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_articles_story ON articles (story_id) WHERE story_id IS NOT NULL"
    )


def downgrade() -> None:
    # pg_trgm stays installed. Dropping an extension is database-wide, so this would be
    # undoing something another migration, or the operator, may be relying on; an unused
    # extension costs nothing beyond a catalog entry.
    op.drop_column("user_article_states", "suppressed_at")
    op.execute("DROP INDEX IF EXISTS ix_articles_story")
    op.execute("DROP INDEX IF EXISTS ix_articles_title_trgm")
    op.drop_column("articles", "story_id")
    op.drop_column("articles", "title_norm")

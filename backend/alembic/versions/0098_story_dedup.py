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

# Kept in sync with app.fetcher.stories — the constants there drive the live path,
# these two only shape the one-off backfill below.
COLLAPSE_THRESHOLD = 0.30
WINDOW_HOURS = 72
MIN_TITLE_CHARS = 12
BACKFILL_DAYS = 30
# Label propagation converges in as many rounds as the longest chain in a group; the
# measured maximum group size is 13, so this is a runaway guard, not a real limit.
MAX_PROPAGATION_ROUNDS = 50


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # immutable_unaccent comes from 0001; lower() on top of it is what pg_trgm would
    # do internally anyway, but storing it keeps the index and the runtime comparison
    # reading the exact same string.
    op.execute("""
        ALTER TABLE articles ADD COLUMN title_norm TEXT
        GENERATED ALWAYS AS (immutable_unaccent(lower(title))) STORED
    """)
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

    _backfill_stories()


def _backfill_stories() -> None:
    """Group the last 30 days of articles, oldest id wins.

    Older articles are left alone: the reading UI never collapses anything outside the
    retention window anyway, and the pair scan is the expensive part of this migration.
    """
    conn = op.get_bind()

    # The `%` operator reads pg_trgm.similarity_threshold, whose default (0.3) happens
    # to match ours — relying on that coincidence is exactly how this breaks later.
    # SET LOCAL keeps it inside the migration's transaction.
    conn.execute(sa.text(f"SET LOCAL pg_trgm.similarity_threshold = {COLLAPSE_THRESHOLD}"))

    # LATERAL, not a plain self-join: it pins the plan to "walk the 30-day window, probe
    # the trigram index once per row", which is what the runtime path does too. Left as a
    # join the planner rates a full sequential scan with similarity() on every row about
    # as cheaply as the index scan, and on a large table that choice is the difference
    # between a minute and an afternoon.
    conn.execute(sa.text(f"""
        CREATE TEMP TABLE story_backfill_pairs ON COMMIT DROP AS
        SELECT a.id AS a_id, b.id AS b_id
        FROM articles a
        CROSS JOIN LATERAL (
            SELECT b.id
            FROM articles b
            WHERE b.id > a.id
              AND b.feed_id IS DISTINCT FROM a.feed_id
              AND b.title_norm % a.title_norm
              AND similarity(a.title_norm, b.title_norm) >= {COLLAPSE_THRESHOLD}
              AND length(b.title_norm) >= {MIN_TITLE_CHARS}
              AND COALESCE(b.published_at, b.fetched_at)
                    BETWEEN COALESCE(a.published_at, a.fetched_at) - INTERVAL '{WINDOW_HOURS} hours'
                        AND COALESCE(a.published_at, a.fetched_at) + INTERVAL '{WINDOW_HOURS} hours'
        ) b
        WHERE COALESCE(a.published_at, a.fetched_at) >= now() - INTERVAL '{BACKFILL_DAYS} days'
          -- Same guard as app.fetcher.stories.MIN_TITLE_CHARS: a trigram score over a
          -- handful of trigrams is noise, and the "Untitled" placeholder would herd
          -- every title-less item into one story.
          AND length(a.title_norm) >= {MIN_TITLE_CHARS}
    """))

    # Every article that matched something starts as its own group, then the minimum
    # id travels along the edges until nothing moves. Cheaper and far more predictable
    # than a recursive CTE over a graph whose shape we don't control.
    conn.execute(sa.text("""
        UPDATE articles SET story_id = id
        WHERE id IN (SELECT a_id FROM story_backfill_pairs
                     UNION SELECT b_id FROM story_backfill_pairs)
    """))

    for _ in range(MAX_PROPAGATION_ROUNDS):
        result = conn.execute(sa.text("""
            UPDATE articles a SET story_id = m.sid
            FROM (
                SELECT id, MIN(sid) AS sid FROM (
                    SELECT p.a_id AS id, other.story_id AS sid
                      FROM story_backfill_pairs p
                      JOIN articles other ON other.id = p.b_id
                    UNION ALL
                    SELECT p.b_id AS id, other.story_id AS sid
                      FROM story_backfill_pairs p
                      JOIN articles other ON other.id = p.a_id
                ) edges GROUP BY id
            ) m
            WHERE a.id = m.id AND a.story_id > m.sid
        """))
        if not result.rowcount:
            break


def downgrade() -> None:
    op.drop_column("user_article_states", "suppressed_at")
    op.execute("DROP INDEX IF EXISTS ix_articles_story")
    op.execute("DROP INDEX IF EXISTS ix_articles_title_trgm")
    op.drop_column("articles", "story_id")
    op.drop_column("articles", "title_norm")

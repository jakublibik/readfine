"""search ignores accents, matches English word forms, and ranks title matches higher

The full-text index from 0022 was built over plain text, so a search for "zpravy"
did not find "zprávy". The index it replaced (0001) did fold accents, and that was
lost when search grew to cover the summary and the readable text. This one folds
them again, with the same immutable_unaccent wrapper title_normalized uses.

Each part goes in twice: as written ('simple') and stemmed ('english'), so "votes"
finds "voting". The query asks both halves and takes either. 'english' alone would
have dropped its stop words, so a search for "The Who" or "IT" would find nothing;
the written half keeps those. Roughly doubles the index.

It also weights the parts: title A, summary B, body D. Matching ignores weights, so
the set of results is the same; ts_rank uses them, so a match in the title ranks
above a word mentioned in passing in a long body.

The expression must stay identical to ``_FTS_VECTOR`` in services/article.py, or
the planner stops using the index and every search scans the table.

Built with the app down, like every migration here, so a plain build.

Revision ID: 0109
Revises: 0108
Create Date: 2026-09-25
"""
from alembic import op

revision = "0109"
down_revision = "0108"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_articles_search_fts")
    op.execute("""
        CREATE INDEX idx_articles_search_fts ON articles
        USING GIN ((
            setweight(to_tsvector('simple', immutable_unaccent(coalesce(title, ''))), 'A') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(title, ''))), 'A') ||
            setweight(to_tsvector('simple', immutable_unaccent(coalesce(summary, ''))), 'B') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(summary, ''))), 'B') ||
            setweight(to_tsvector('simple', immutable_unaccent(coalesce(content, '') || ' ' || coalesce(readable_content, ''))), 'D') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(content, '') || ' ' || coalesce(readable_content, ''))), 'D')
        ))
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_articles_search_fts")
    op.execute("""
        CREATE INDEX idx_articles_search_fts ON articles
        USING GIN (
            to_tsvector('simple',
                coalesce(title, '') || ' ' ||
                coalesce(summary, '') || ' ' ||
                coalesce(content, '') || ' ' ||
                coalesce(readable_content, '')
            )
        )
    """)

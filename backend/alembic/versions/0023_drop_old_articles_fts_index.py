"""Drop obsolete ix_articles_fts index superseded by idx_articles_search_fts

Revision ID: 0023
Revises: 0022
Create Date: 2026-04-01
"""
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_articles_fts")


def downgrade() -> None:
    op.execute(
        "CREATE INDEX ix_articles_fts ON articles USING GIN "
        "(to_tsvector('simple', immutable_unaccent(title) || ' ' || immutable_unaccent(COALESCE(content, ''))))"
    )

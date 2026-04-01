"""Add GIN index for article full-text search

Revision ID: 0022
Revises: 0021
Create Date: 2026-04-01
"""
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_articles_search_fts")

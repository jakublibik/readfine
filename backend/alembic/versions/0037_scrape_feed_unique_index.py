"""Fix unique index on feeds to allow multiple public scrape feeds per URL with different selectors

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-08
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_feeds_url_public")
    op.execute(
        "CREATE UNIQUE INDEX ix_feeds_url_public_non_scrape "
        "ON feeds (feed_url) WHERE is_private = FALSE AND feed_type != 'scrape'"
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_feeds_scrape_url_selector "
        "ON feeds (feed_url, (type_config->>'article_links_selector')) "
        "WHERE is_private = FALSE AND feed_type = 'scrape'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_feeds_url_public_non_scrape")
    op.execute("DROP INDEX IF EXISTS ix_feeds_scrape_url_selector")
    op.execute(
        "CREATE UNIQUE INDEX ix_feeds_url_public ON feeds (feed_url) WHERE is_private = FALSE"
    )

"""Add url_normalized column to articles for cross-feed deduplication

Revision ID: 0031
Revises: 0030
Create Date: 2026-05-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("url_normalized", sa.String(2048), nullable=True))
    op.execute(
        "UPDATE articles SET url_normalized = lower(rtrim(url, '/')) WHERE url IS NOT NULL"
    )
    op.create_index("ix_articles_url_normalized", "articles", ["url_normalized"])


def downgrade() -> None:
    op.drop_index("ix_articles_url_normalized", table_name="articles")
    op.drop_column("articles", "url_normalized")

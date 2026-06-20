"""Rename content_source value feed_content → feed_full

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-03
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE articles SET content_source = 'feed_full' WHERE content_source = 'feed_content'")


def downgrade() -> None:
    op.execute("UPDATE articles SET content_source = 'feed_content' WHERE content_source = 'feed_full'")

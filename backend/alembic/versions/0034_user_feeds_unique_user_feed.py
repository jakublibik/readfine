"""Add unique constraint on user_feeds(user_id, feed_id)

Revision ID: 0034
Revises: 0033
Create Date: 2026-04-30
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_user_feeds_user_feed", "user_feeds", ["user_id", "feed_id"])


def downgrade() -> None:
    op.drop_constraint("uq_user_feeds_user_feed", "user_feeds", type_="unique")

"""drop user_feeds.unread_count

The cached unread column was no longer read for display: both the web sidebar
and the API compute unread counts fresh from the DB. It was still maintained on
write paths (fetch increments, mark-read decrements, dedup/retention recalcs),
inconsistently, so it could drift. Drop it; every response now counts on read.

Revision ID: 0079
Revises: 0078
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user_feeds", "unread_count")


def downgrade() -> None:
    op.add_column(
        "user_feeds",
        sa.Column(
            "unread_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("user_feeds", "unread_count", server_default=None)

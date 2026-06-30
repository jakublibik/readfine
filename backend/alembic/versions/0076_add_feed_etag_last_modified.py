"""add feeds.etag + feeds.last_modified

Stores HTTP validators (ETag / Last-Modified) returned by a feed so the next fetch
can send them as If-None-Match / If-Modified-Since. An unchanged feed then answers
304 Not Modified with no body — lighter on the server (and its rate limit) and on
our bandwidth/parsing. Nullable: feeds we have not fetched yet have no validators.

Revision ID: 0076
Revises: 0075
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feeds", sa.Column("etag", sa.String(length=255), nullable=True))
    op.add_column("feeds", sa.Column("last_modified", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("feeds", "last_modified")
    op.drop_column("feeds", "etag")

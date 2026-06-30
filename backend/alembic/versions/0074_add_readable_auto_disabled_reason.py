"""add user_feeds.readable_auto_disabled_reason

Records *why* readable extraction was auto-disabled so the UI can show an accurate
message. Three flows set ``readable_auto_disabled``: full-content detection (the feed
already delivers full text — a benign disable), repeated HTTP 403s, and repeated empty
extractions (e.g. a bot-verification wall). Without a reason they all rendered the same
misleading "the site blocked content extraction" notice. Nullable: legacy rows and
not-auto-disabled feeds have no reason.

Revision ID: 0074
Revises: 0073
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_feeds",
        sa.Column("readable_auto_disabled_reason", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_feeds", "readable_auto_disabled_reason")

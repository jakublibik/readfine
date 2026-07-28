"""add readable-extraction revival bookkeeping to feeds

A feed that answers 403 three times in a row has readable extraction turned off for
every subscriber, and nothing ever turns it back on: only a manual save in the feed
edit form clears the flags. That made a temporary block permanent — the switch to
HTTP/2 fixed a whole class of those 403s, yet the feeds stayed disabled.

These columns let a daily job probe such feeds twice (after 3 and 14 days) and revive
them when the block is gone. ``readable_revival_attempts`` is cumulative over the
feed's lifetime and deliberately never reset by the job: a feed re-disabled after a
successful probe proves the probe lied, and resetting would loop it forever.

The backfill schedules a probe for feeds that are disabled *and* have an article
carrying a 403 error. That second condition matters because the 'blocked' reason is
shared with feeds disabled for extracting nothing: those would pass an HTTP probe,
revive, and be disabled again by the empty-extraction detector. Probe times are spread
over a week so the whole backlog does not land in one run.

Revision ID: 0084
Revises: 0083
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column("readable_revival_next_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "feeds",
        sa.Column(
            "readable_revival_attempts",
            sa.SmallInteger(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "feeds",
        sa.Column("readable_revived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE feeds f
        SET readable_revival_next_at = now() + random() * interval '7 days'
        WHERE EXISTS (
            SELECT 1 FROM user_feeds uf
            WHERE uf.feed_id = f.id
              AND uf.readable_auto_disabled = TRUE
              AND uf.readable_auto_disabled_reason = 'blocked'
              AND uf.extract_readable = FALSE
        )
        AND EXISTS (
            SELECT 1 FROM articles a
            WHERE a.feed_id = f.id
              AND a.readable_error LIKE 'HTTP 403%'
        )
        """
    )


def downgrade() -> None:
    op.drop_column("feeds", "readable_revived_at")
    op.drop_column("feeds", "readable_revival_attempts")
    op.drop_column("feeds", "readable_revival_next_at")

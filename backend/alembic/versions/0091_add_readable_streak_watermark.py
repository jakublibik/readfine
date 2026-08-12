"""add a watermark so readable 403/empty streaks only count articles since re-enable

The cross-batch streak checks in readable_service take the feed's newest terminal
articles and disable readable extraction when they all carry the same failure. The
window had no lower bound, and disabling leaves those articles alone, so after readable
was turned back on the old failures were still the newest terminal rows: one fresh 403
was enough to re-disable the feed, turning a threshold of 3 (and 5 for empty
extractions) into 1.

This column records where the streak may start counting. It is stamped with the feed's
newest article id every time readable comes back on, and the streak queries ignore
anything at or below it. NULL means "never re-enabled", i.e. count everything, which is
what every existing row wants — hence no backfill.

An article id rather than a timestamp because a success has to be able to break a
streak and articles carry no success timestamp, only readable_failed_at.

Revision ID: 0091
Revises: 0090
Create Date: 2026-08-12
"""
from alembic import op
import sqlalchemy as sa

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column("readable_streak_from_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("feeds", "readable_streak_from_id")

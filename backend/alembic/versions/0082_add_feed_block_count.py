"""add feeds.block_count and reset the learned host spacing cache

``block_count`` tracks consecutive fetches refused by the host itself (anti-bot
403, bare 429). Those were previously counted as fetch errors and disabled the
feed after five in a row, even though they are unrelated to the feed's health.

The same migration empties ``host_rate_limits``. Values learned from a 200
response were derived as ``reset / remaining``, which is only a rate when the
budget still has room; with ``remaining = 0`` the ``reset`` header is the phase
left in the current window, so the ratchet was learning sampling noise. Rows
carrying a good value and rows carrying that artifact are indistinguishable
after the fact (``source`` records "learned from a 200", not what ``remaining``
was), and 429-derived values are frequently a multiple of a 200-derived base, so
a targeted cleanup is not possible.

The table is a self-healing cache: it refills from the next fetch of each host,
and the worst case is a short window of slightly too-fast fetching that the host
corrects with a 429 anyway. Nothing durable is lost, so downgrade is a no-op.

Revision ID: 0082
Revises: 0081
Create Date: 2026-07-21
"""
from alembic import op
import sqlalchemy as sa

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feeds",
        sa.Column("block_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("DELETE FROM host_rate_limits")


def downgrade() -> None:
    op.drop_column("feeds", "block_count")

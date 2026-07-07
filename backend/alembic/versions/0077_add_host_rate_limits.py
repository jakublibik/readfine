"""add host_rate_limits

Persists the learned per-host fetch spacing (see app.fetcher.host_throttle) so an
aggressive host like Reddit isn't re-probed into a 429 after every restart/deploy.
Keyed by host, since spacing is a property of the host's rate limit, not a feed.

Revision ID: 0077
Revises: 0076
Create Date: 2026-07-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "host_rate_limits",
        sa.Column("host", sa.String(length=255), primary_key=True),
        sa.Column("spacing_seconds", sa.Float(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("consecutive_429", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("learned_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("host_rate_limits")

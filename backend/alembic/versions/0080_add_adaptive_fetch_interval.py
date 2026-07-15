"""add adaptive fetch interval columns

feeds.derived_interval_min: auto (adaptive) poll interval derived from the feed's
publish cadence (app.fetcher.interval). NULL = not enough history → global default.
Backfilled at app startup by the daily recompute, so no data migration here.

app_settings.max_fetch_interval_min: upper cap for the auto interval (default 360 min).

Revision ID: 0080
Revises: 0079
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("feeds", sa.Column("derived_interval_min", sa.SmallInteger(), nullable=True))
    op.add_column(
        "app_settings",
        sa.Column(
            "max_fetch_interval_min",
            sa.SmallInteger(),
            nullable=False,
            server_default="360",
        ),
    )
    op.alter_column("app_settings", "max_fetch_interval_min", server_default=None)


def downgrade() -> None:
    op.drop_column("app_settings", "max_fetch_interval_min")
    op.drop_column("feeds", "derived_interval_min")

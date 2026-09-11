"""aggregated visit counts for the public pages

Three tables and a flag, all off by default. Nothing is recorded until an admin
turns traffic_stats_enabled on.

Views are hourly and additive, so they can be folded into days in any timezone at
query time. Visitors are daily because they are not additive at all: the day a
visit belongs to has to be decided when it is recorded, in one fixed timezone.

Both hourly primary keys start with `hour`, which covers the window range scans
the admin page does, so neither needs an extra index.

Revision ID: 0097
Revises: 0096
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_view_hourly",
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("path", sa.String(length=40), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bot_views", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("hour", "path"),
    )
    op.create_table(
        "traffic_source_hourly",
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("hour", "source"),
    )
    op.create_table(
        "visitor_daily",
        # path '*' is the whole instance, anything else a single page — the two
        # overlap, so a total must filter path = '*'. See models/traffic.py.
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("path", sa.String(length=40), nullable=False),
        sa.Column("visitors", sa.Integer(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("day", "path"),
    )
    op.add_column(
        "app_settings",
        sa.Column(
            "traffic_stats_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "traffic_stats_enabled")
    op.drop_table("visitor_daily")
    op.drop_table("traffic_source_hourly")
    op.drop_table("page_view_hourly")

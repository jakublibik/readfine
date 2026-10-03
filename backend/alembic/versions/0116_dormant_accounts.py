"""Pause fetching for accounts nobody uses (dormant accounts)

app_settings.dormant_after_days: days without activity after which an account stops
keeping its feeds fetched. NULL = off, the default, also on upgrade.
app_settings.dormant_warning_enabled: email a warning 7 days before (needs SMTP).
users.pause_warning_sent_at: when that warning went out. Only counts while it is
newer than the account's last activity, so it is never reset.

Revision ID: 0116
Revises: 0115
Create Date: 2026-09-29
"""
import sqlalchemy as sa
from alembic import op

revision = "0116"
down_revision = "0115"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("dormant_after_days", sa.SmallInteger(), nullable=True))
    op.add_column("app_settings", sa.Column(
        "dormant_warning_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("pause_warning_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "pause_warning_sent_at")
    op.drop_column("app_settings", "dormant_warning_enabled")
    op.drop_column("app_settings", "dormant_after_days")

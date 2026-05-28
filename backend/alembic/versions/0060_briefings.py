"""add briefing columns to user_catchup_configs

Revision ID: 0060
Revises: 0059
Create Date: 2026-05-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_catchup_configs", sa.Column("briefing_enabled", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_catchup_configs", sa.Column("briefing_interval", sa.String(10), nullable=True))
    op.add_column("user_catchup_configs", sa.Column("briefing_day", sa.SmallInteger(), nullable=True))
    op.add_column("user_catchup_configs", sa.Column("briefing_time", sa.String(5), nullable=True))
    op.add_column("user_catchup_configs", sa.Column("briefing_recipients", sa.Text(), nullable=True))
    op.add_column("user_catchup_configs", sa.Column("briefing_last_sent_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_catchup_configs", sa.Column("briefing_last_error", sa.Text(), nullable=True))
    op.add_column("user_catchup_configs", sa.Column("briefing_retry_count", sa.SmallInteger(), nullable=False, server_default="0"))
    op.add_column("user_catchup_configs", sa.Column("briefing_next_send_at", sa.DateTime(timezone=True), nullable=True))

    op.create_check_constraint(
        "ck_user_catchup_configs_briefing_day",
        "user_catchup_configs",
        "briefing_day IS NULL OR (briefing_day >= 0 AND briefing_day <= 6)",
    )

    op.create_index(
        "ix_user_catchup_configs_briefing_next_send_at",
        "user_catchup_configs",
        ["briefing_next_send_at"],
        postgresql_where=sa.text("briefing_enabled = true"),
    )


def downgrade() -> None:
    op.drop_index("ix_user_catchup_configs_briefing_next_send_at", table_name="user_catchup_configs")
    op.drop_constraint("ck_user_catchup_configs_briefing_day", "user_catchup_configs", type_="check")
    for col in [
        "briefing_next_send_at", "briefing_retry_count", "briefing_last_error",
        "briefing_last_sent_at", "briefing_recipients", "briefing_time",
        "briefing_day", "briefing_interval", "briefing_enabled",
    ]:
        op.drop_column("user_catchup_configs", col)

"""add automatic interest-profile regeneration fields

Adds the schedule (``ai_preference_auto_days``: 0 = off, 14 or 28 days), the
bookkeeping needed to show when the profile last changed regardless of the
schedule, the previous text kept for one-click revert, and the error state
surfaced in settings and on the admin dashboard.

The two NOT NULL columns land on a populated table, so they are added with a
server_default that is dropped again right after the backfill.

Revision ID: 0083
Revises: 0082
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("ai_preference_auto_days", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.alter_column("user_settings", "ai_preference_auto_days", server_default=None)
    op.add_column(
        "user_settings",
        sa.Column("ai_preference_fail_count", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.alter_column("user_settings", "ai_preference_fail_count", server_default=None)
    op.add_column("user_settings", sa.Column("ai_preference_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_settings", sa.Column("ai_preference_source", sa.String(length=10), nullable=True))
    op.add_column("user_settings", sa.Column("ai_preference_last_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_settings", sa.Column("ai_preference_prev_text", sa.Text(), nullable=True))
    op.add_column("user_settings", sa.Column("ai_preference_last_error", sa.Text(), nullable=True))
    op.add_column("user_settings", sa.Column("ai_preference_last_error_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for column in (
        "ai_preference_last_error_at",
        "ai_preference_last_error",
        "ai_preference_prev_text",
        "ai_preference_last_attempt_at",
        "ai_preference_source",
        "ai_preference_updated_at",
        "ai_preference_fail_count",
        "ai_preference_auto_days",
    ):
        op.drop_column("user_settings", column)

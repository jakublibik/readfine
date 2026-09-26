"""welcome screen and the relevance intro bar

onboarded_at is set once a new account has been through /welcome, where it is
asked which topics it follows. Every account that exists before this migration
is stamped here, so the screen only ever greets accounts created after it.

relevance_intro_dismissed_at records the reader closing the bar in the article
list that points at Settings → Relevance. Kept in the database rather than the
browser so that closing it once closes it on every device.

Revision ID: 0108
Revises: 0107
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("onboarded_at", sa.DateTime(timezone=True)))
    op.add_column("user_settings", sa.Column("relevance_intro_dismissed_at", sa.DateTime(timezone=True)))
    op.execute("UPDATE user_settings SET onboarded_at = now()")


def downgrade() -> None:
    op.drop_column("user_settings", "relevance_intro_dismissed_at")
    op.drop_column("user_settings", "onboarded_at")

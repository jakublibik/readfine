"""track when a profile's lexical backfill last ran

Saving an interest profile has to pull the recent unread articles up to it, or the
profile only starts meaning something tomorrow. The trigger is this timestamp against
ai_preference_updated_at: older means the account is due. Storing it that way rather
than enqueuing a task means a run that dies halfway is simply still due, and a profile
saved twice in a minute is one backfill, not two.

NULL on every existing account, so each one with a profile gets caught up once, over
the same 7-day window a new save would use.

Revision ID: 0104
Revises: 0103
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("lexical_backfill_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "lexical_backfill_at")

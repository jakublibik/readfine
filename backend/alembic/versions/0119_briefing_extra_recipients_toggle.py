"""add app_settings.briefing_extra_recipients_enabled

Whether briefings may go to addresses other than the account's own. The extra
recipients never confirm anything and the digest's wording follows the user's
own prompt, so on an open instance they let any account send mail of its
choosing from the instance's domain. Default false: an admin who wants them
turns them on. Admins themselves are not held to it.

Stored recipients are left in place, so switching the toggle back on restores
them as they were.

Revision ID: 0119
Revises: 0118
Create Date: 2026-10-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0119"
down_revision = "0118"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "briefing_extra_recipients_enabled", sa.Boolean(),
            server_default="false", nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "briefing_extra_recipients_enabled")

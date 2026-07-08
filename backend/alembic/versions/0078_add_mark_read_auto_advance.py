"""add user_settings.mark_read_auto_advance

Opt-in behaviour: after marking a feed, folder or label read from the sidebar,
automatically open the next one that still has unread articles. Off by default,
so existing rows get ``false``.

Revision ID: 0078
Revises: 0077
Create Date: 2026-07-08
"""
from alembic import op
import sqlalchemy as sa

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "mark_read_auto_advance",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("user_settings", "mark_read_auto_advance", server_default=None)


def downgrade() -> None:
    op.drop_column("user_settings", "mark_read_auto_advance")

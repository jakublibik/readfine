"""add the open-original-for-empty-articles preference

Articles with no readable text and no feed content render an empty detail pane.
With this preference on, clicking such an article opens the source in a new tab
instead. Off by default, so nobody's reading flow changes on upgrade.

The column is NOT NULL on a populated table, so it lands with a server_default
that is dropped again right after.

Revision ID: 0085
Revises: 0084
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("open_original_when_empty", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.alter_column("user_settings", "open_original_when_empty", server_default=None)


def downgrade() -> None:
    op.drop_column("user_settings", "open_original_when_empty")

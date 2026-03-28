"""replace show_unread_only bool with unread_filter string

Revision ID: 0011
Revises: 0010
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'user_settings',
        sa.Column('unread_filter', sa.String(20), nullable=False, server_default='adaptive'),
    )
    # Migrate existing values: show_unread_only=true → unread_only, false → show_all
    op.execute(
        "UPDATE user_settings SET unread_filter = CASE "
        "WHEN show_unread_only = true THEN 'unread_only' "
        "ELSE 'show_all' END"
    )
    op.drop_column('user_settings', 'show_unread_only')


def downgrade() -> None:
    op.add_column(
        'user_settings',
        sa.Column('show_unread_only', sa.Boolean(), nullable=False, server_default='true'),
    )
    op.execute(
        "UPDATE user_settings SET show_unread_only = CASE "
        "WHEN unread_filter = 'unread_only' THEN true "
        "ELSE false END"
    )
    op.drop_column('user_settings', 'unread_filter')

"""rename last_login_at to last_active_at on users

Revision ID: 0009
Revises: 0008
Create Date: 2026-03-28
"""
from alembic import op

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('users', 'last_login_at', new_column_name='last_active_at')


def downgrade() -> None:
    op.alter_column('users', 'last_active_at', new_column_name='last_login_at')

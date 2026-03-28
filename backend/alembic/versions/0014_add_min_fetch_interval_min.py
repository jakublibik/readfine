"""add min_fetch_interval_min to app_settings

Revision ID: 0014
Revises: 0013
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'app_settings',
        sa.Column('min_fetch_interval_min', sa.SmallInteger(), nullable=False, server_default='15'),
    )


def downgrade() -> None:
    op.drop_column('app_settings', 'min_fetch_interval_min')

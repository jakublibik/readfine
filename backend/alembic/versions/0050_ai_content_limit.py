"""add ai_content_limit to user_settings

Revision ID: 0050
Revises: 0049
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa

revision = '0050'
down_revision = '0049'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_settings',
        sa.Column('ai_content_limit', sa.Integer(), nullable=False, server_default='20000'),
    )


def downgrade():
    op.drop_column('user_settings', 'ai_content_limit')

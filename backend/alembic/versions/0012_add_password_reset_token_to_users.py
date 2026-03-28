"""add password_reset_token to users

Revision ID: 0012
Revises: 0011
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('password_reset_token', sa.String(64), nullable=True, unique=True))
    op.add_column('users', sa.Column('password_reset_expires_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'password_reset_expires_at')
    op.drop_column('users', 'password_reset_token')

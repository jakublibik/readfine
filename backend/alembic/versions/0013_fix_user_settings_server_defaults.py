"""fix user_settings server defaults for density columns

Revision ID: 0013
Revises: 0012
Create Date: 2026-03-28
"""
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column('user_settings', 'list_density_web',
                    server_default='comfortable', existing_nullable=False)
    op.alter_column('user_settings', 'list_density_mobile',
                    server_default='comfortable', existing_nullable=False)


def downgrade() -> None:
    op.alter_column('user_settings', 'list_density_web',
                    server_default='comfortable', existing_nullable=False)
    op.alter_column('user_settings', 'list_density_mobile',
                    server_default='compact', existing_nullable=False)

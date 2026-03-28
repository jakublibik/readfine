"""rename list_density_web default medium to comfortable

Revision ID: 0010
Revises: 0009
Create Date: 2026-03-28
"""
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE user_settings SET list_density_web = 'comfortable' WHERE list_density_web = 'medium'")


def downgrade() -> None:
    op.execute("UPDATE user_settings SET list_density_web = 'medium' WHERE list_density_web = 'comfortable'")

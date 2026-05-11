"""Add key_prefix to user_ai_keys

Revision ID: 0040
Revises: 0039
Create Date: 2026-05-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_ai_keys", sa.Column("key_prefix", sa.String(12), nullable=True))


def downgrade():
    op.drop_column("user_ai_keys", "key_prefix")

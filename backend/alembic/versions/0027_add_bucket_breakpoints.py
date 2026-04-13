"""Add bucket breakpoints to user_settings

Revision ID: 0027
Revises: 0026
Create Date: 2026-04-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("bucket_small_max", sa.SmallInteger(), nullable=False, server_default="640"))
    op.add_column("user_settings", sa.Column("bucket_medium_max", sa.SmallInteger(), nullable=False, server_default="1100"))


def downgrade() -> None:
    op.drop_column("user_settings", "bucket_medium_max")
    op.drop_column("user_settings", "bucket_small_max")

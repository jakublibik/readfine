"""Add unique constraint on folders(user_id, name)

Revision ID: 0021
Revises: 0020
Create Date: 2026-03-31
"""
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint("uq_folders_user_name", "folders", ["user_id", "name"])


def downgrade() -> None:
    op.drop_constraint("uq_folders_user_name", "folders", type_="unique")

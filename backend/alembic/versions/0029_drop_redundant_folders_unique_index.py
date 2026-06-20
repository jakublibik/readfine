"""Drop redundant unique index on folders(user_id, name)

The unique constraint uq_folders_user_name (added in 0021) already enforces
this rule. The original unique index ix_folders_user_name from 0001 is redundant.

Revision ID: 0029
Revises: 0028
Create Date: 2026-04-22
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_folders_user_name", table_name="folders")


def downgrade() -> None:
    op.create_index("ix_folders_user_name", "folders", ["user_id", "name"], unique=True)

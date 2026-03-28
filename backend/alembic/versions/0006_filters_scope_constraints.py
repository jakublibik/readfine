"""Add CHECK constraints for filter scope field consistency.

Revision ID: 0006
Revises: 0005
Create Date: 2026-03-28
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # scope_type='feed'   => scope_feed_id NOT NULL, scope_folder_id NULL
    # scope_type='folder' => scope_folder_id NOT NULL, scope_feed_id NULL
    # scope_type='all'    => both NULL
    op.create_check_constraint(
        "ck_filters_scope_fields",
        "filters",
        "(scope_type = 'all'    AND scope_feed_id IS NULL    AND scope_folder_id IS NULL) OR"
        "(scope_type = 'feed'   AND scope_feed_id IS NOT NULL AND scope_folder_id IS NULL) OR"
        "(scope_type = 'folder' AND scope_folder_id IS NOT NULL AND scope_feed_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_filters_scope_fields", "filters")

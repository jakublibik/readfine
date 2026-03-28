"""Add scope columns to filters table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("filters", sa.Column(
        "scope_type", sa.String(10), nullable=False, server_default="all"
    ))
    op.create_check_constraint(
        "ck_filters_scope_type", "filters",
        "scope_type IN ('all', 'feed', 'folder')"
    )
    op.add_column("filters", sa.Column(
        "scope_feed_id", sa.Integer,
        sa.ForeignKey("feeds.id", ondelete="SET NULL"), nullable=True
    ))
    op.add_column("filters", sa.Column(
        "scope_folder_id", sa.Integer,
        sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True
    ))


def downgrade() -> None:
    op.drop_column("filters", "scope_folder_id")
    op.drop_column("filters", "scope_feed_id")
    op.drop_constraint("ck_filters_scope_type", "filters")
    op.drop_column("filters", "scope_type")

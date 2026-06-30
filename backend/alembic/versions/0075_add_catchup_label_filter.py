"""add user_catchup_configs.label_filter

Catch me up replaces the single "Labeled only" relevance toggle with the reusable
label selector (any / specific labels, JSON array — same shape as search's
``label_filter``). The new column stores that selection. Existing configs that had
``filter_labeled`` on are backfilled to ``["any"]`` so they keep matching "has at
least one label". The legacy ``filter_labeled`` column is left in place (no longer
read or written) for a later cleanup.

Revision ID: 0075
Revises: 0074
Create Date: 2026-06-30
"""
from alembic import op
import sqlalchemy as sa

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_catchup_configs",
        sa.Column("label_filter", sa.Text(), nullable=True),
    )
    op.execute(
        "UPDATE user_catchup_configs SET label_filter = '[\"any\"]' "
        "WHERE filter_labeled = true"
    )


def downgrade() -> None:
    op.drop_column("user_catchup_configs", "label_filter")

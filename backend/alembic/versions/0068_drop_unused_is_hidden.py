"""drop unused user_article_states.is_hidden

The column was added in 0001 for a planned "hide" filter action that was never
implemented: it was only ever written as its default (False) and never read or
filtered anywhere. Retention uses Article.trimmed_at instead. Removing dead schema.

Revision ID: 0068
Revises: 0067
Create Date: 2026-06-18
"""
from alembic import op
import sqlalchemy as sa

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("user_article_states", "is_hidden")


def downgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("is_hidden", sa.Boolean(), nullable=False, server_default="false"),
    )

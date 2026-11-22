"""mark articles whose filters wait for the reader's translation of the title

A reader who translates a feed writes filters in their own language, so at fetch
their filters for that feed's new articles wait for the translated title instead of
running over the original. This flag is the waiting: set at fetch, cleared once the
filters ran, over the translation or, when no translation is coming, over the
original. Partial index, like ix_uas_relevance_filters_pending (0107), so the
minute job that looks for these rows reads only the waiting ones.

False on every existing row, which is right: nothing was ever deferred before.
Adding a column with a constant default does not rewrite the table.

Revision ID: 0128
Revises: 0127
Create Date: 2026-11-21
"""
import sqlalchemy as sa
from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("translation_filters_pending", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )
    op.create_index(
        "ix_uas_translation_filters_pending",
        "user_article_states",
        ["user_id"],
        postgresql_where=sa.text("translation_filters_pending"),
    )


def downgrade() -> None:
    op.drop_index("ix_uas_translation_filters_pending", table_name="user_article_states")
    op.drop_column("user_article_states", "translation_filters_pending")

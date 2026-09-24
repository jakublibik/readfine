"""mark articles whose relevance filters wait for the AI score

A filter on the relevance score (AI, else basic) runs exactly once per article,
when the number it reads is final. At fetch that is right away, unless a label
filter just sent the article to AI scoring; then the filter waits for the AI score,
and falls back to the basic score if the scoring never lands. This flag is the
waiting: set at fetch, cleared by whichever of the two runs the filters. Without it
an AI score arriving later (a label added by hand) would run the same filters a
second time.

False on every existing row, which is right: nothing was ever deferred before.
Adding a column with a constant default does not rewrite the table.

Revision ID: 0106
Revises: 0105
Create Date: 2026-09-24
"""
from alembic import op
import sqlalchemy as sa

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("relevance_filters_pending", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("user_article_states", "relevance_filters_pending")

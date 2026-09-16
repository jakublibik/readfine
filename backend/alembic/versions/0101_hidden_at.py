"""story dedup: keep a record of an article having been hidden

``suppressed_at`` / ``suppressed_by`` say whether an article is hidden right now, and
every human read clears them (0098 and the handlers around it) — it has to, or an
article the reader has since read could never count as "I have seen this" and the next
instalment of the same news would be taken away too.

That makes them useless as a record. The settings list of hidden articles was built on
them and emptied itself as it was read: half a minute in front of an article, which is
what checking one takes, and the row was gone from the list and off the counter — even
where the reader had just decided the call was wrong.

So the record gets a column of its own. Written once by the suppression rule and never
cleared, it outlives the reader's reading and disappears with the article itself.

Nullable ADD COLUMN, so no table rewrite: unlike 0098 this is instant on any size.

Revision ID: 0101
Revises: 0100
Create Date: 2026-09-16
"""
import sqlalchemy as sa
from alembic import op

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("hidden_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The list reads one reader's last seven days, so it asks by user and by date.
    op.create_index(
        "ix_user_article_states_hidden",
        "user_article_states",
        ["user_id", "hidden_at"],
        postgresql_where=sa.text("hidden_at IS NOT NULL"),
    )
    # What is still hidden was hidden, so it can be carried over. One pass over the
    # table writing a handful of rows: the reads that are still standing at this moment,
    # which on a week's worth is a couple of dozen per reader. Nothing brings back the
    # ones already read — those cleared the column this is copied from, which is the
    # whole reason the new one exists.
    op.execute(
        "UPDATE user_article_states SET hidden_at = suppressed_at "
        "WHERE suppressed_by = 'similar' AND suppressed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_user_article_states_hidden", table_name="user_article_states")
    op.drop_column("user_article_states", "hidden_at")

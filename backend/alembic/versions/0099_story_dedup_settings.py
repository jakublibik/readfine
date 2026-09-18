"""story dedup: the per-user setting and who wrote a machine read

story_dedup turns the feature down ('off') or up ('collapse_suppress'). Everyone,
existing accounts included, starts on 'collapse', which is what shipped in 0098:
coverage of one story folds into a single row and says how many sources are behind it.
Suppression, which hides an article for repeating one the reader already read, is
opt-in — the match is lexical and cannot tell another source on the same event from the
next development in it.

suppressed_by records which machine wrote a suppressed_at, since by now four different
ones do. Only 'similar' is the opt-in suppression, and the counter in settings has to
show that one alone or the number the reader watches the threshold by would also count
URL duplicates, filter actions and stories they finished themselves. Backfilled as NULL
on purpose: the rows already in the table cannot be told apart any more, and guessing a
reason for them would put made-up numbers in that counter.

Revision ID: 0099
Revises: 0098
Create Date: 2026-09-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("story_dedup", sa.String(20), nullable=False,
                  server_default="collapse"),
    )
    op.add_column(
        "user_article_states",
        sa.Column("suppressed_by", sa.String(12), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_article_states", "suppressed_by")
    op.drop_column("user_settings", "story_dedup")

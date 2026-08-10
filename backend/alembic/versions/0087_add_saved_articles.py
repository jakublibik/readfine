"""save an article by URL: the Saved marker and its filter bookkeeping

Until now an article could only enter Readfine through a feed. Saving a pasted URL
produces an article with no feed attached, which needs somewhere to record "this
user saved this" — starred and archived were the only per-user markers, and reusing
either would have drained them of meaning.

saved_at is that marker. It also carries visibility (the Saved view), access (see
article_access_predicate) and the exemption from retention purge, so a saved article
survives on its own rather than by borrowing a star.

filters_applied_at makes the post-extraction pass idempotent per (article, user).
The terminal-state check alone is not enough: a dedup save by a second user can flip
an already-finished article back to pending and re-extract it, and the article would
then pass through a terminal state twice, re-applying the first user's filter actions
(star/archive/mark-read) after they had undone them.

Both columns are nullable, so no server_default dance is needed. The partial index
is the Saved view's only access path.

Revision ID: 0087
Revises: 0086
Create Date: 2026-08-04
"""
from alembic import op
import sqlalchemy as sa

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "user_article_states",
        sa.Column("filters_applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_uas_user_saved",
        "user_article_states",
        ["user_id", sa.text("saved_at DESC")],
        postgresql_where=sa.text("saved_at IS NOT NULL"),
    )
    # The new column starts entirely NULL, so until the table is analysed the planner
    # has no selectivity estimate for "saved_at IS NOT NULL" and badly overestimates
    # the match count. Measured on a 19k-article dev database: without this it walked
    # every article through ix_articles_sort_ts (cost ~13800) instead of using the
    # index above (cost ~16). Autovacuum would get there eventually; this makes the
    # Saved view fast from the first request after deploy.
    op.execute("ANALYZE user_article_states")


def downgrade() -> None:
    op.drop_index("ix_uas_user_saved", table_name="user_article_states")
    op.drop_column("user_article_states", "filters_applied_at")
    op.drop_column("user_article_states", "saved_at")

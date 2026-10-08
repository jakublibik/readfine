"""delete labels on articles their owner can no longer reach

Unsubscribing, unsaving and unstarring used to leave the user's labels behind. The
label view lists by label alone, so those articles stayed in it while the detail,
the read toggle and mark-all-read refused them, and the label's unread badge never
cleared. The services now drop such labels as access goes (see
services.article.drop_unreachable_labels); this clears the ones left from before.

Reachable means the same as article_access_predicate: subscribed to the article's
feed, or keeping it for good (starred, archived or saved).

Revision ID: 0120
Revises: 0119
Create Date: 2026-10-04
"""
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM article_labels al
        WHERE NOT EXISTS (
            SELECT 1 FROM user_feeds uf
            JOIN articles a ON a.feed_id = uf.feed_id
            WHERE a.id = al.article_id AND uf.user_id = al.user_id
        )
        AND NOT EXISTS (
            SELECT 1 FROM user_article_states s
            WHERE s.article_id = al.article_id AND s.user_id = al.user_id
              AND (s.is_starred OR s.is_archived OR s.saved_at IS NOT NULL)
        )
    """)


def downgrade() -> None:
    # The deleted labels pointed at articles their owner could not open; nothing to restore.
    pass

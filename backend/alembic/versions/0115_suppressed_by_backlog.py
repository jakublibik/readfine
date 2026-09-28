"""user_article_states.suppressed_by: allow 'backlog'

A reader who subscribes to a feed the instance already has gets its older articles
marked read, stamped 'backlog' (services.feed._mark_backlog_read), so they count as
no one's reading. The check constraint from 0100 lists the values one by one.

Revision ID: 0115
Revises: 0114
Create Date: 2026-09-28
"""
from alembic import op

revision = "0115"
down_revision = "0114"
branch_labels = None
depends_on = None

_NAME = "ck_user_article_states_suppressed_by"


def upgrade() -> None:
    op.drop_constraint(_NAME, "user_article_states", type_="check")
    op.create_check_constraint(
        _NAME, "user_article_states",
        "suppressed_by IN ('url', 'filter', 'story', 'bulk', 'similar', 'backlog')",
    )


def downgrade() -> None:
    # Back to how those articles were before: unread.
    op.execute("""
        UPDATE user_article_states
        SET is_read = false, read_at = NULL, suppressed_at = NULL, suppressed_by = NULL
        WHERE suppressed_by = 'backlog'
    """)
    op.drop_constraint(_NAME, "user_article_states", type_="check")
    op.create_check_constraint(
        _NAME, "user_article_states",
        "suppressed_by IN ('url', 'filter', 'story', 'bulk', 'similar')",
    )

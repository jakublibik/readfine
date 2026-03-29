"""add index for readable pending queue

Revision ID: 0018
Revises: 0017
Create Date: 2026-03-29
"""
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE INDEX ix_articles_readable_pending
        ON articles (readable_next_retry_at, id)
        WHERE readable_status = 'pending'
        """
    )


def downgrade() -> None:
    op.drop_index("ix_articles_readable_pending", table_name="articles")

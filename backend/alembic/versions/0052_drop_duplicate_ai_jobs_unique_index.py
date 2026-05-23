"""article_ai_jobs: drop duplicate unique index (keep unique constraint)

Revision ID: 0052
Revises: 0051
Create Date: 2026-05-23
"""
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_article_ai_jobs_article_user_op", table_name="article_ai_jobs")


def downgrade() -> None:
    op.create_index(
        "ix_article_ai_jobs_article_user_op",
        "article_ai_jobs",
        ["article_id", "user_id", "operation"],
        unique=True,
    )

"""article_ai_jobs: add unique constraint (article_id, user_id, operation)

Revision ID: 0045
Revises: 0044
Create Date: 2026-05-13
"""
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_article_ai_jobs_article_user_op",
        "article_ai_jobs",
        ["article_id", "user_id", "operation"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_article_ai_jobs_article_user_op", "article_ai_jobs")

"""user_article_states: add CHECK constraint for ai_score range

Revision ID: 0053
Revises: 0052
Create Date: 2026-05-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_user_article_states_ai_score_range",
        "user_article_states",
        sa.text("ai_score IS NULL OR (ai_score >= 0.0 AND ai_score <= 1.0)"),
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_user_article_states_ai_score_range",
        "user_article_states",
    )

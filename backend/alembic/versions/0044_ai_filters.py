"""ai_filters_applied in user_article_states

Revision ID: 0044
Revises: 0043
Create Date: 2026-05-13
"""
import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column(
            "ai_filters_applied",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_uas_ai_filters_pending",
        "user_article_states",
        ["user_id"],
        postgresql_where=sa.text("ai_score IS NOT NULL AND ai_filters_applied = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_uas_ai_filters_pending", table_name="user_article_states")
    op.drop_column("user_article_states", "ai_filters_applied")

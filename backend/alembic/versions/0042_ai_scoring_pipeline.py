"""AI scoring pipeline: article_ai_jobs table, ai_score in user_article_states, ai_score_show_in_list in user_settings

Revision ID: 0042
Revises: 0041
Create Date: 2026-05-11
"""
import sqlalchemy as sa
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "article_ai_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("article_id", sa.BigInteger(), sa.ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("operation IN ('scoring', 'summary', 'context')", name="ck_article_ai_jobs_operation"),
        sa.CheckConstraint("status IN ('pending', 'success', 'failed', 'skipped')", name="ck_article_ai_jobs_status"),
    )
    op.create_index("ix_article_ai_jobs_pending", "article_ai_jobs",
                    ["status", "next_retry_at", "id"],
                    postgresql_where=sa.text("status = 'pending'"))
    op.create_index("ix_article_ai_jobs_article_user_op", "article_ai_jobs",
                    ["article_id", "user_id", "operation"], unique=True)

    op.add_column("user_article_states", sa.Column("ai_score", sa.Float(), nullable=True))
    op.add_column("user_settings", sa.Column("ai_score_show_in_list", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("user_settings", "ai_score_show_in_list")
    op.drop_column("user_article_states", "ai_score")
    op.drop_index("ix_article_ai_jobs_article_user_op", table_name="article_ai_jobs")
    op.drop_index("ix_article_ai_jobs_pending", table_name="article_ai_jobs")
    op.drop_table("article_ai_jobs")

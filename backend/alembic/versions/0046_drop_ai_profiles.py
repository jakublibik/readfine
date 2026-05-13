"""Drop unused ai_profiles table; add index on article_ai_jobs for scheduler query

Revision ID: 0046
Revises: 0045
Create Date: 2026-05-13
"""
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_table("ai_profiles")
    op.create_index(
        "ix_article_ai_jobs_scheduler",
        "article_ai_jobs",
        ["operation", "status", "next_retry_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_article_ai_jobs_scheduler", "article_ai_jobs")
    op.create_table(
        "ai_profiles",
        *_ai_profiles_columns(),
    )


def _ai_profiles_columns():
    import sqlalchemy as sa
    return [
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=True),
        sa.Column("max_tokens", sa.Integer(), nullable=True),
        sa.Column("summary_language", sa.String(10), nullable=False, server_default="en"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]

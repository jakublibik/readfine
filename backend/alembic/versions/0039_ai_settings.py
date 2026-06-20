"""AI settings: user_settings columns, user_feeds flags, remove legacy articles AI columns

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-10
"""
import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # user_settings: AI model slots, preference text, defaults, error tracking
    op.add_column("user_settings", sa.Column("ai_fast_provider", sa.String(30), nullable=True))
    op.add_column("user_settings", sa.Column("ai_fast_model", sa.String(100), nullable=True))
    op.add_column("user_settings", sa.Column("ai_quality_provider", sa.String(30), nullable=True))
    op.add_column("user_settings", sa.Column("ai_quality_model", sa.String(100), nullable=True))
    op.add_column("user_settings", sa.Column("ai_preference_text", sa.Text(), nullable=True))
    op.add_column("user_settings", sa.Column("ai_scoring_enabled_default", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("ai_summary_enabled_default", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_settings", sa.Column("last_ai_error", sa.Text(), nullable=True))
    op.add_column("user_settings", sa.Column("last_ai_error_at", sa.DateTime(timezone=True), nullable=True))

    # user_feeds: per-feed AI overrides (null = use global default)
    op.add_column("user_feeds", sa.Column("ai_scoring_enabled", sa.Boolean(), nullable=True))
    op.add_column("user_feeds", sa.Column("ai_summary_enabled", sa.Boolean(), nullable=True))

    # articles: remove legacy Phase 2 AI columns (never used, moving to user_article_states)
    op.drop_column("articles", "ai_summary")
    op.drop_column("articles", "ai_score")
    op.drop_column("articles", "ai_tags_suggested")
    op.drop_column("articles", "ai_processed_at")


def downgrade() -> None:
    op.add_column("articles", sa.Column("ai_processed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("articles", sa.Column("ai_tags_suggested", sa.ARRAY(sa.String()), nullable=True))
    op.add_column("articles", sa.Column("ai_score", sa.Float(), nullable=True))
    op.add_column("articles", sa.Column("ai_summary", sa.Text(), nullable=True))

    op.drop_column("user_feeds", "ai_summary_enabled")
    op.drop_column("user_feeds", "ai_scoring_enabled")

    op.drop_column("user_settings", "last_ai_error_at")
    op.drop_column("user_settings", "last_ai_error")
    op.drop_column("user_settings", "ai_summary_enabled_default")
    op.drop_column("user_settings", "ai_scoring_enabled_default")
    op.drop_column("user_settings", "ai_preference_text")
    op.drop_column("user_settings", "ai_quality_model")
    op.drop_column("user_settings", "ai_quality_provider")
    op.drop_column("user_settings", "ai_fast_model")
    op.drop_column("user_settings", "ai_fast_provider")

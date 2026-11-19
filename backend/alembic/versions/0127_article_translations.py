"""AI title translation: storage, per-feed switch, target language and model slot

article_translations: one translated title per (reader, article, language). Not
shared between readers: each pays for their own with their own key. The original
stays in articles untouched.
user_feeds.translate: the reader translates this feed. Off by default.
user_settings.ai_translation_language: the target language code. NULL = off.
user_settings.ai_translation_slot: which model slot translates, 'fast' (scoring) by
default or 'quality' (main).
article_ai_jobs: the operation CHECK gains 'translation'.

Revision ID: 0127
Revises: 0126
Create Date: 2026-11-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0127"
down_revision = "0126"
branch_labels = None
depends_on = None

_OPS_OLD = "operation IN ('scoring', 'summary', 'context')"
_OPS_NEW = "operation IN ('scoring', 'summary', 'context', 'translation')"


def upgrade() -> None:
    op.create_table(
        "article_translations",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("article_id", sa.BigInteger(), sa.ForeignKey("articles.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("language", sa.String(10), primary_key=True),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )
    # The PK leads with user_id; the article purge deletes by article_id.
    op.create_index("ix_article_translations_article_id", "article_translations", ["article_id"])

    op.add_column("user_feeds", sa.Column(
        "translate", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("user_settings", sa.Column("ai_translation_language", sa.String(10), nullable=True))
    op.add_column("user_settings", sa.Column(
        "ai_translation_slot", sa.String(10), nullable=False, server_default="fast"))

    op.drop_constraint("ck_article_ai_jobs_operation", "article_ai_jobs", type_="check")
    op.create_check_constraint("ck_article_ai_jobs_operation", "article_ai_jobs", _OPS_NEW)


def downgrade() -> None:
    op.execute("DELETE FROM article_ai_jobs WHERE operation = 'translation'")
    op.drop_constraint("ck_article_ai_jobs_operation", "article_ai_jobs", type_="check")
    op.create_check_constraint("ck_article_ai_jobs_operation", "article_ai_jobs", _OPS_OLD)

    op.drop_column("user_settings", "ai_translation_slot")
    op.drop_column("user_settings", "ai_translation_language")
    op.drop_column("user_feeds", "translate")
    op.drop_index("ix_article_translations_article_id", table_name="article_translations")
    op.drop_table("article_translations")

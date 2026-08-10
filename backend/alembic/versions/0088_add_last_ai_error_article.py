"""link the last AI error to the article it happened on

The error panel in settings shows only the message, so a failure that is specific
to one article (empty provider response, content the model refuses) looks exactly
like a broken API key. Recording which article the failed job was working on lets
the panel link to it.

Nullable: interest-profile errors have no article, and retention purge may remove
the article before anyone reads the error, hence ON DELETE SET NULL rather than
CASCADE: losing the article should drop the link, not the error.

Revision ID: 0088
Revises: 0087
Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("last_ai_error_article_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_settings_last_ai_error_article_id",
        "user_settings",
        "articles",
        ["last_ai_error_article_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_user_settings_last_ai_error_article_id", "user_settings", type_="foreignkey"
    )
    op.drop_column("user_settings", "last_ai_error_article_id")

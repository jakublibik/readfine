"""user_article_states: add ever_starred, starred_at + stats indexes

Revision ID: 0054
Revises: 0053
Create Date: 2026-05-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_article_states", sa.Column("ever_starred", sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("user_article_states", sa.Column("starred_at", sa.DateTime(timezone=True), nullable=True))

    # Migrate existing data: articles that were manually starred get ever_starred = true
    op.execute("UPDATE user_article_states SET ever_starred = TRUE WHERE user_starred = TRUE")

    # Indexes for stats queries
    op.create_index(
        "ix_uas_user_read_at",
        "user_article_states",
        ["user_id", "read_at"],
        postgresql_where=sa.text("read_at IS NOT NULL"),
    )
    op.create_index(
        "ix_uas_user_dwell",
        "user_article_states",
        ["user_id", "dwell_seconds"],
    )
    op.create_index(
        "ix_uas_user_ever_starred",
        "user_article_states",
        ["user_id", "ever_starred"],
        postgresql_where=sa.text("ever_starred = TRUE"),
    )


def downgrade() -> None:
    op.drop_index("ix_uas_user_ever_starred", table_name="user_article_states")
    op.drop_index("ix_uas_user_dwell", table_name="user_article_states")
    op.drop_index("ix_uas_user_read_at", table_name="user_article_states")
    op.drop_column("user_article_states", "starred_at")
    op.drop_column("user_article_states", "ever_starred")

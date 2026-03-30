"""Add check constraints for purge retention columns

Revision ID: 0019
Revises: 0018
Create Date: 2026-03-30
"""
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE app_settings ADD CONSTRAINT ck_app_settings_purge_after_days "
        "CHECK (default_purge_after_days IS NULL OR default_purge_after_days >= 1)"
    )
    op.execute(
        "ALTER TABLE app_settings ADD CONSTRAINT ck_app_settings_purge_keep_count "
        "CHECK (default_purge_keep_count IS NULL OR default_purge_keep_count >= 1)"
    )
    op.execute(
        "ALTER TABLE user_feeds ADD CONSTRAINT ck_user_feeds_purge_after_days "
        "CHECK (purge_after_days IS NULL OR purge_after_days >= 1)"
    )
    op.execute(
        "ALTER TABLE user_feeds ADD CONSTRAINT ck_user_feeds_purge_keep_count "
        "CHECK (purge_keep_count IS NULL OR purge_keep_count >= 1)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE user_feeds DROP CONSTRAINT ck_user_feeds_purge_keep_count")
    op.execute("ALTER TABLE user_feeds DROP CONSTRAINT ck_user_feeds_purge_after_days")
    op.execute("ALTER TABLE app_settings DROP CONSTRAINT ck_app_settings_purge_keep_count")
    op.execute("ALTER TABLE app_settings DROP CONSTRAINT ck_app_settings_purge_after_days")

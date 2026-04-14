"""Add CHECK constraints for bucket breakpoints

Revision ID: 0028
Revises: 0027
Create Date: 2026-04-14
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_user_settings_bucket_small_max_range",
        "user_settings",
        "bucket_small_max BETWEEN 320 AND 1000",
    )
    op.create_check_constraint(
        "ck_user_settings_bucket_medium_max_range",
        "user_settings",
        "bucket_medium_max BETWEEN 420 AND 2000",
    )
    op.create_check_constraint(
        "ck_user_settings_bucket_gap",
        "user_settings",
        "bucket_small_max + 100 <= bucket_medium_max",
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_settings_bucket_gap", "user_settings")
    op.drop_constraint("ck_user_settings_bucket_medium_max_range", "user_settings")
    op.drop_constraint("ck_user_settings_bucket_small_max_range", "user_settings")

"""Add reading_font_size and reading_font_family to user_settings

Revision ID: 0030
Revises: 0029
Create Date: 2026-04-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("reading_font_size", sa.String(10), nullable=False, server_default="md"))
    op.add_column("user_settings", sa.Column("reading_font_family", sa.String(10), nullable=False, server_default="sans"))
    op.create_check_constraint(
        "ck_user_settings_reading_font_size",
        "user_settings",
        "reading_font_size IN ('sm', 'md', 'lg')",
    )
    op.create_check_constraint(
        "ck_user_settings_reading_font_family",
        "user_settings",
        "reading_font_family IN ('sans', 'serif')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_user_settings_reading_font_size", "user_settings", type_="check")
    op.drop_constraint("ck_user_settings_reading_font_family", "user_settings", type_="check")
    op.drop_column("user_settings", "reading_font_family")
    op.drop_column("user_settings", "reading_font_size")

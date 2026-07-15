"""add user_settings.format_profile

Per-user number & date format profile (variant B). Existing rows are backfilled
to ``eu`` (Europe: comma decimal, dd.mm.yyyy) via server_default; the app-side
default is the neutral ``iso`` for freshly seeded rows and fallbacks.

Revision ID: 0081
Revises: 0080
Create Date: 2026-07-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column(
            "format_profile",
            sa.String(length=10),
            nullable=False,
            server_default="eu",
        ),
    )
    op.alter_column("user_settings", "format_profile", server_default=None)


def downgrade() -> None:
    op.drop_column("user_settings", "format_profile")

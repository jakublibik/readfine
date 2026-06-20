"""registration disabled by default

Flip app_settings.registration_enabled server default from true to false so
fresh self-hosted installs start with registration closed (secure by default;
the admin opens it in the admin panel for a public instance). Existing rows are
deliberately left untouched, so running instances keep their current setting.

Revision ID: 0070
Revises: 0069
Create Date: 2026-06-20
"""
from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("app_settings", "registration_enabled", server_default="false")


def downgrade() -> None:
    op.alter_column("app_settings", "registration_enabled", server_default="true")

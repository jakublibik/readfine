"""legal fields in app_settings

Revision ID: 0063
Revises: 0062
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("app_settings", sa.Column("legal_operator_name", sa.String(255), nullable=True))
    op.add_column("app_settings", sa.Column("legal_contact_email", sa.String(255), nullable=True))
    op.add_column("app_settings", sa.Column("legal_jurisdiction", sa.String(100), nullable=True))
    op.add_column("app_settings", sa.Column("legal_last_updated", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("app_settings", "legal_last_updated")
    op.drop_column("app_settings", "legal_jurisdiction")
    op.drop_column("app_settings", "legal_contact_email")
    op.drop_column("app_settings", "legal_operator_name")

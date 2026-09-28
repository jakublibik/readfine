"""users.signup_source: where an account came from when it signed up

Only the referring site's domain, a utm tag, or one of direct, internal and invite.
Written only while public page counting is on, and cleared for everyone when it is
switched off (traffic_service.apply_enabled). NULL means not known.

Revision ID: 0112
Revises: 0111
Create Date: 2026-09-28
"""
import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("signup_source", sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "signup_source")

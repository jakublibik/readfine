"""Add ON DELETE RESTRICT to invitations.created_by FK

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-25
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Re-create FK with explicit ON DELETE RESTRICT (PostgreSQL default NO ACTION
    # is effectively the same, but this makes the intent explicit)
    op.drop_constraint("invitations_created_by_fkey", "invitations", type_="foreignkey")
    op.create_foreign_key(
        "invitations_created_by_fkey",
        "invitations", "users",
        ["created_by"], ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("invitations_created_by_fkey", "invitations", type_="foreignkey")
    op.create_foreign_key(
        "invitations_created_by_fkey",
        "invitations", "users",
        ["created_by"], ["id"],
    )

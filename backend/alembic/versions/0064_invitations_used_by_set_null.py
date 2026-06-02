"""invitations.used_by ON DELETE SET NULL

Revision ID: 0064
Revises: 0063
Create Date: 2026-06-02
"""
from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("invitations_used_by_fkey", "invitations", type_="foreignkey")
    op.create_foreign_key(
        "invitations_used_by_fkey",
        "invitations",
        "users",
        ["used_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("invitations_used_by_fkey", "invitations", type_="foreignkey")
    op.create_foreign_key(
        "invitations_used_by_fkey",
        "invitations",
        "users",
        ["used_by"],
        ["id"],
    )

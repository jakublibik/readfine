"""Add CHECK constraints for enum-like columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-24
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD CONSTRAINT ck_users_role "
        "CHECK (role IN ('admin', 'user'))"
    )
    op.execute(
        "ALTER TABLE filters ADD CONSTRAINT ck_filters_match_operator "
        "CHECK (match_operator IN ('AND', 'OR'))"
    )
    op.execute(
        "ALTER TABLE filter_actions ADD CONSTRAINT ck_filter_actions_action_type "
        "CHECK (action_type IN ('label', 'mark_read', 'star', 'hide', 'notify'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE filter_actions DROP CONSTRAINT ck_filter_actions_action_type")
    op.execute("ALTER TABLE filters DROP CONSTRAINT ck_filters_match_operator")
    op.execute("ALTER TABLE users DROP CONSTRAINT ck_users_role")

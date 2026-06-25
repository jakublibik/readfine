"""allow 'archive' in filter_actions.action_type

Adds the new filter action ``archive`` to the CHECK constraint introduced in
0002. The constraint is dropped and recreated with the extra value (Postgres has
no ALTER CONSTRAINT for CHECK). Reserved values 'hide'/'notify' are preserved.

Revision ID: 0072
Revises: 0071
Create Date: 2026-06-24
"""
from alembic import op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE filter_actions DROP CONSTRAINT ck_filter_actions_action_type")
    op.execute(
        "ALTER TABLE filter_actions ADD CONSTRAINT ck_filter_actions_action_type "
        "CHECK (action_type IN ('label', 'mark_read', 'star', 'archive', 'hide', 'notify'))"
    )


def downgrade() -> None:
    # Drop any rows using the new value so the narrower constraint can be re-applied.
    op.execute("DELETE FROM filter_actions WHERE action_type = 'archive'")
    op.execute("ALTER TABLE filter_actions DROP CONSTRAINT ck_filter_actions_action_type")
    op.execute(
        "ALTER TABLE filter_actions ADD CONSTRAINT ck_filter_actions_action_type "
        "CHECK (action_type IN ('label', 'mark_read', 'star', 'hide', 'notify'))"
    )

"""Drop "add label" filter actions whose label is gone

Deleting a label used to leave its filter actions behind: they did nothing, the
filter list showed a bare id, and saving such a filter failed. label_service now
removes them with the label; this clears the ones left from before. A filter left
with no action at all is switched off, as the delete path does.

Revision ID: 0123
Revises: 0122
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()
    emptied = conn.execute(sa.text("""
        WITH dropped AS (
            DELETE FROM filter_actions fa
            USING filters f
            WHERE fa.filter_id = f.id
              AND fa.action_type = 'label'
              AND NOT EXISTS (
                  SELECT 1 FROM labels l
                  WHERE l.id::text = fa.action_value AND l.user_id = f.user_id
              )
            RETURNING fa.filter_id
        )
        SELECT DISTINCT filter_id FROM dropped
    """)).scalars().all()
    if emptied:
        conn.execute(sa.text("""
            UPDATE filters f SET is_active = false
            WHERE f.id = ANY(:ids)
              AND NOT EXISTS (SELECT 1 FROM filter_actions fa WHERE fa.filter_id = f.id)
        """), {"ids": list(emptied)})


def downgrade() -> None:
    # The dropped actions pointed at labels that no longer exist; nothing to restore.
    pass

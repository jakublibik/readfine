"""autovacuum the busiest tables sooner

The default thresholds wait for a fifth of a table to be dead rows (a tenth changed
for analyze). On articles that is ~50k rows, against ~6k a nightly purge deletes, so
production went six days between vacuums and the visibility map went stale enough
for index-only scans to fall back to the heap. Scaled down per table, these run about
daily at that inflow, and on a small install they still wait for the base threshold
of 50 rows.

Only sets storage parameters: no rewrite, and the lock is held for an instant.

Revision ID: 0126
Revises: 0125
Create Date: 2026-10-18
"""
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None

_TABLES = ("articles", "user_article_states", "article_labels")
_PARAMS = {
    "autovacuum_vacuum_scale_factor": "0.02",
    "autovacuum_vacuum_insert_scale_factor": "0.05",
    "autovacuum_analyze_scale_factor": "0.05",
}


def upgrade() -> None:
    settings = ", ".join(f"{k} = {v}" for k, v in _PARAMS.items())
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} SET ({settings})")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} RESET ({', '.join(_PARAMS)})")

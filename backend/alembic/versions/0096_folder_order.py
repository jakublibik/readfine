"""per-user folder order, alphabetical or manual

Folder.position has existed since the first schema but nothing in the web UI
ever set it, so every row carries the default 0. Backfilling it with the current
alphabetical order (all folders, empty ones included) means positions describe
what users already see the moment the setting appears, and the reordering code
can assume a dense 1..N list instead of normalizing zeroes on first use.

folders_arranged records whether the user has ever moved a folder by hand. Until
they have, switching to the manual order seeds it from the alphabetical order on
screen, so nothing jumps; afterwards the arrangement is theirs and switching
views leaves it alone.

Positions set through the API are overwritten. They had no effect on any view
until now, so nothing that was visible changes.

Revision ID: 0096
Revises: 0095
Create Date: 2026-09-10
"""
from alembic import op
import sqlalchemy as sa

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("folder_order", sa.String(length=10), nullable=False, server_default="name"),
    )
    op.add_column(
        "user_settings",
        sa.Column("folders_arranged", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        """
        UPDATE folders AS f
        SET position = ranked.rn
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY user_id ORDER BY lower(name), id
            ) AS rn
            FROM folders
        ) AS ranked
        WHERE f.id = ranked.id AND f.position IS DISTINCT FROM ranked.rn
        """
    )


def downgrade() -> None:
    # Positions stay as they are: they are valid values for a column that
    # predates this migration.
    op.drop_column("user_settings", "folders_arranged")
    op.drop_column("user_settings", "folder_order")

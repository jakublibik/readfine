"""catchup config unique constraint: (user_id, name, period)

Revision ID: 0061
Revises: 0060
Create Date: 2026-05-28
"""
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("uq_user_catchup_configs_user_name", "user_catchup_configs", type_="unique")
    op.create_unique_constraint(
        "uq_user_catchup_configs_user_name_period",
        "user_catchup_configs",
        ["user_id", "name", "period"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_user_catchup_configs_user_name_period", "user_catchup_configs", type_="unique")
    op.create_unique_constraint(
        "uq_user_catchup_configs_user_name",
        "user_catchup_configs",
        ["user_id", "name"],
    )

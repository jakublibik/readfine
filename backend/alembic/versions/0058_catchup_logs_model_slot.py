"""add model_slot column to catchup_logs

Revision ID: 0058
Revises: 0057
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catchup_logs",
        sa.Column("model_slot", sa.String(10), nullable=False, server_default="fast"),
    )
    # Remove the server_default after backfilling so future rows must provide the value explicitly
    op.alter_column("catchup_logs", "model_slot", server_default=None)


def downgrade() -> None:
    op.drop_column("catchup_logs", "model_slot")

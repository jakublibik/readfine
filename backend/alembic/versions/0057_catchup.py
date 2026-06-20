"""add user_catchup_configs and catchup_logs tables

Revision ID: 0057
Revises: 0056
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_catchup_configs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("scope_include", sa.Text(), nullable=True),
        sa.Column("period", sa.String(20), nullable=False, server_default="7days"),
        sa.Column("filter_status", sa.String(20), nullable=False, server_default="all"),
        sa.Column("filter_labeled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("filter_score_min", sa.Float(), nullable=True),
        sa.Column("article_limit", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("model_slot", sa.String(10), nullable=False, server_default="fast"),
        sa.Column("custom_prompt", sa.Text(), nullable=True),
        sa.Column("include_snippet", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_catchup_configs_user_name"),
    )
    op.create_index("ix_user_catchup_configs_user_id", "user_catchup_configs", ["user_id"])

    op.create_table(
        "catchup_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("config_id", sa.Integer(), nullable=True),
        sa.Column("article_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("provider", sa.String(50), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["config_id"], ["user_catchup_configs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_catchup_logs_user_id", "catchup_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_catchup_logs_user_id", "catchup_logs")
    op.drop_table("catchup_logs")
    op.execute("ALTER TABLE user_catchup_configs DROP CONSTRAINT IF EXISTS uq_user_catchup_configs_user_name")
    op.drop_index("ix_user_catchup_configs_user_id", "user_catchup_configs")
    op.drop_table("user_catchup_configs")

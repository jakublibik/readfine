"""add ai_custom_base_url to user_settings

Endpoint for the "custom" AI provider, which speaks the OpenAI protocol to
something other than OpenAI: Ollama, llama.cpp, vLLM, LiteLLM, OpenRouter.

Nullable with no default: the column only means anything once someone picks the
custom provider, and every existing row is on a hosted provider with a fixed
endpoint.

Revision ID: 0093
Revises: 0092
Create Date: 2026-08-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_settings",
        sa.Column("ai_custom_base_url", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("user_settings", "ai_custom_base_url")

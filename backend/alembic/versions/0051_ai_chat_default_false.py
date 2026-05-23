"""ai_chat_enabled default false

Revision ID: 0051
Revises: 0050
Create Date: 2026-05-23
"""
from alembic import op

revision = '0051'
down_revision = '0050'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE user_settings ALTER COLUMN ai_chat_enabled SET DEFAULT false")
    op.execute("UPDATE user_settings SET ai_chat_enabled = false")


def downgrade():
    op.execute("ALTER TABLE user_settings ALTER COLUMN ai_chat_enabled SET DEFAULT true")

"""add link_opened to user_article_states

Revision ID: 0047
Revises: 0046
Create Date: 2026-05-14
"""
from alembic import op
import sqlalchemy as sa

revision = '0047'
down_revision = '0046'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'user_article_states',
        sa.Column('link_opened', sa.Boolean(), nullable=False, server_default='false'),
    )


def downgrade():
    op.drop_column('user_article_states', 'link_opened')

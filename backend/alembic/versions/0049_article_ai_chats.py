"""add article_ai_chats and ai_chat_enabled

Revision ID: 0049
Revises: 0048
Create Date: 2026-05-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0049'
down_revision = '0048'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'article_ai_chats',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('article_id', sa.BigInteger(),
                  sa.ForeignKey('articles.id', ondelete='CASCADE'), nullable=False),
        sa.Column('messages', postgresql.JSONB(), nullable=False, server_default='[]'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('user_id', 'article_id', name='uq_article_ai_chats_user_article'),
    )
    op.create_index('ix_article_ai_chats_user_id', 'article_ai_chats', ['user_id'])
    op.add_column('user_settings',
        sa.Column('ai_chat_enabled', sa.Boolean(), nullable=False, server_default='true'))


def downgrade():
    op.drop_column('user_settings', 'ai_chat_enabled')
    op.drop_index('ix_article_ai_chats_user_id', table_name='article_ai_chats')
    op.drop_table('article_ai_chats')

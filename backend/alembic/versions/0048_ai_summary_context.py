"""add AI summary/context fields

Revision ID: 0048
Revises: 0047
Create Date: 2026-05-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0048'
down_revision = '0047'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('user_article_states', sa.Column('ai_summary', sa.Text(), nullable=True))
    op.add_column('user_article_states', sa.Column('ai_context', sa.Text(), nullable=True))
    op.add_column('user_settings', sa.Column('ai_summary_prompt', sa.Text(), nullable=True))
    op.add_column('user_settings', sa.Column('ai_context_prompt', sa.Text(), nullable=True))
    op.add_column('article_ai_jobs', sa.Column('job_params', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('article_ai_jobs', 'job_params')
    op.drop_column('user_settings', 'ai_context_prompt')
    op.drop_column('user_settings', 'ai_summary_prompt')
    op.drop_column('user_article_states', 'ai_context')
    op.drop_column('user_article_states', 'ai_summary')

"""Drop the unused feeds.favicon_url and feeds.favicon_data columns

Nothing has ever written to them. The feeds settings page and the API's feed
response still read favicon_url, always null; left in place, a later change that
filled it from feed data would have the settings page load an image from a
feed's own server.

Revision ID: 0124
Revises: 0123
Create Date: 2026-10-06
"""
import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("feeds", "favicon_data")
    op.drop_column("feeds", "favicon_url")


def downgrade() -> None:
    op.add_column("feeds", sa.Column("favicon_url", sa.String(2048)))
    op.add_column("feeds", sa.Column("favicon_data", sa.Text))

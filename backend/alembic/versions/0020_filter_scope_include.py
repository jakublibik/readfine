"""Replace filter scope columns with scope_include JSON list

Revision ID: 0020
Revises: 0019
Create Date: 2026-03-31
"""
import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new column
    op.add_column("filters", sa.Column("scope_include", sa.Text(), nullable=True))

    # Migrate existing data
    op.execute("""
        UPDATE filters
        SET scope_include = CASE
            WHEN scope_type = 'feed'   THEN '["feed:'   || scope_feed_id::text   || '"]'
            WHEN scope_type = 'folder' THEN '["folder:' || scope_folder_id::text || '"]'
            ELSE NULL
        END
    """)

    # Drop check constraint from migration 0006
    op.execute("ALTER TABLE filters DROP CONSTRAINT ck_filters_scope_fields")

    # Drop old columns
    op.drop_column("filters", "scope_type")
    op.drop_column("filters", "scope_feed_id")
    op.drop_column("filters", "scope_folder_id")


def downgrade() -> None:
    op.add_column("filters", sa.Column("scope_type", sa.String(10), nullable=False, server_default="all"))
    op.add_column("filters", sa.Column("scope_feed_id", sa.Integer(), nullable=True))
    op.add_column("filters", sa.Column("scope_folder_id", sa.Integer(), nullable=True))

    # Restore single-value scope from scope_include (first element only).
    # Rows with multi-item or unparseable scope_include fall back to 'all'.
    # Regex guards ensure we only CAST values that are valid integers.
    op.execute("""
        UPDATE filters
        SET
            scope_type = CASE
                WHEN scope_include ~ E'^\\["feed:\\d+"\\]$'   THEN 'feed'
                WHEN scope_include ~ E'^\\["folder:\\d+"\\]$' THEN 'folder'
                ELSE 'all'
            END,
            scope_feed_id = CASE
                WHEN scope_include ~ E'^\\["feed:\\d+"\\]$'
                THEN split_part(scope_include::jsonb->>0, ':', 2)::integer
                ELSE NULL
            END,
            scope_folder_id = CASE
                WHEN scope_include ~ E'^\\["folder:\\d+"\\]$'
                THEN split_part(scope_include::jsonb->>0, ':', 2)::integer
                ELSE NULL
            END
    """)

    op.execute(
        "ALTER TABLE filters ADD CONSTRAINT ck_filters_scope_fields CHECK ("
        "(scope_type = 'all'    AND scope_feed_id IS NULL    AND scope_folder_id IS NULL) OR "
        "(scope_type = 'feed'   AND scope_feed_id IS NOT NULL AND scope_folder_id IS NULL) OR "
        "(scope_type = 'folder' AND scope_folder_id IS NOT NULL AND scope_feed_id IS NULL)"
        ")"
    )

    op.drop_column("filters", "scope_include")

"""article_labels: add FK from (label_id, user_id) to labels (id, user_id) for tenant consistency

Revision ID: 0007
Revises: 0006
Create Date: 2026-03-28
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL requires the referenced columns to be covered by a UNIQUE or PK constraint.
    # labels.id is already PK, but the composite (id, user_id) needs its own UNIQUE.
    op.create_unique_constraint("uq_labels_id_user_id", "labels", ["id", "user_id"])
    # Add composite FK: article_labels.(label_id, user_id) → labels.(id, user_id)
    # This ensures both columns reference the same label row owned by the same user.
    op.create_foreign_key(
        "fk_article_labels_label_user",
        "article_labels",
        "labels",
        ["label_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_article_labels_label_user", "article_labels", type_="foreignkey")
    op.drop_constraint("uq_labels_id_user_id", "labels", type_="unique")

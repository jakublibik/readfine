"""story dedup: pin down the two columns that hold a fixed set of words

Both arrived in 0098/0099 without a constraint, which the rest of the schema does not
do: label_display got one in 0025, the reading fonts in 0030, article_ai_jobs.status
and .operation in the model itself.

story_dedup is validated in the settings route, so the constraint is a second line
rather than the first. suppressed_by has no such single door: five different places
write it (the URL dedup, a filter action, a closed story, mark all read, the
similarity rule) and the settings counter reads exactly one of the five, so a value
misspelled at any one of them would not raise anything, it would quietly stop being
counted. NULL stays legal, and means a read the reader made themselves.

Separate from 0099 rather than folded into it, because 0099 has already run on
development and staging databases and an edited migration would not replay there.

Revision ID: 0100
Revises: 0099
Create Date: 2026-09-15
"""
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_user_settings_story_dedup",
        "user_settings",
        "story_dedup IN ('off', 'collapse', 'collapse_suppress')",
    )
    op.create_check_constraint(
        "ck_user_article_states_suppressed_by",
        "user_article_states",
        "suppressed_by IN ('url', 'filter', 'story', 'bulk', 'similar')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_user_article_states_suppressed_by", "user_article_states", type_="check"
    )
    op.drop_constraint("ck_user_settings_story_dedup", "user_settings", type_="check")

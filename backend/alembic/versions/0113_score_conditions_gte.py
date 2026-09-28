"""score conditions: "more than N" becomes "at least N + 1"

The filter editor now offers the search's two score operators, at least (gte) and
below (lt), compared on the whole number a list shows. gt N already compares that
way as at least N + 1 (filter_service._score_matches), so rewriting it changes no
result, only how the condition reads in the editor. gt 100 can never match and is
left as it is, since at least 101 would not pass the editor's 0 to 100 check.
equals is left alone too: it never matched a score, and turning it into at least
would start a filter that has done nothing so far.

Revision ID: 0113
Revises: 0112
Create Date: 2026-09-28
"""
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None

_SCORE_FIELDS = "('ai_score', 'basic_score', 'relevance_score')"
_NUMBER = r"'^\s*[0-9]+(\.[0-9]+)?\s*$'"


def upgrade() -> None:
    op.execute(f"""
        UPDATE filter_conditions
        SET operator = 'gte', value = (floor(trim(value)::numeric) + 1)::int::text
        WHERE field IN {_SCORE_FIELDS} AND operator = 'gt'
          AND value ~ {_NUMBER} AND trim(value)::numeric < 100
    """)


def downgrade() -> None:
    # The code before this revision does not know gte. gt N - 1 is the same
    # condition in whole numbers.
    op.execute(f"""
        UPDATE filter_conditions
        SET operator = 'gt', value = (ceil(trim(value)::numeric) - 1)::int::text
        WHERE field IN {_SCORE_FIELDS} AND operator = 'gte' AND value ~ {_NUMBER}
    """)

"""search finds words in Thai, Lao, Khmer and Burmese text

These scripts put no spaces between words either, so they get the same overlapping
character pairs as Chinese, Japanese and Korean (migration 0117). The pairs are of
code points, vowel signs included, which a dictionary would do better; the query
goes through the same cutting, so a word still matches where it stands as written.
The function keeps its name, since it is part of the index expression.

The index has to be rebuilt only where the new function gives another result, and
that is text with one of these scripts in it. Without any, every stored entry is
still what the new function would compute, so the rebuild is skipped: the check
reads the table once (0.7 s for 34k articles on dev), the rebuild takes minutes.

Revision ID: 0125
Revises: 0124
Create Date: 2026-10-09
"""
from alembic import op

revision = "0125"
down_revision = "0124"
branch_labels = None
depends_on = None

_CJK = "぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿가-힯ᄀ-ᇿ㄰-㆏"
# Thai and Lao, Myanmar, Khmer.
_SEA = "฀-໿က-႟ក-៿"


def _create_function(chars: str) -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION cjk_bigrams(t text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT CASE WHEN t !~ '[{chars}]' THEN t ELSE (
            SELECT string_agg(
              CASE WHEN m[1] IS NULL THEN m[2]
                   WHEN length(m[1]) = 1 THEN m[1]
                   ELSE (SELECT string_agg(substr(m[1], i, 2), ' ' ORDER BY i)
                         FROM generate_series(1, length(m[1]) - 1) i)
              END, ' ' ORDER BY n)
            FROM regexp_matches(t, '([{chars}]+)|([^{chars}]+)', 'g')
                 WITH ORDINALITY AS r(m, n))
          END
        $$
    """)


def _reindex_if_any_text_changed() -> None:
    op.execute(f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM articles WHERE concat_ws(' ', title, summary,
                     content, readable_content) ~ '[{_SEA}]') THEN
            REINDEX INDEX idx_articles_search_fts;
          END IF;
        END $$
    """)


def upgrade() -> None:
    _create_function(_CJK + _SEA)
    _reindex_if_any_text_changed()


def downgrade() -> None:
    _create_function(_CJK)
    _reindex_if_any_text_changed()

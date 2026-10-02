"""search finds words in Chinese, Japanese and Korean text

The 'simple' parser splits only on spaces and punctuation. Chinese and Japanese
don't put spaces between words, so a whole sentence up to the next comma was one
lexeme and a word inside it could not be found; Korean words with a particle
attached had the same problem.

cjk_bigrams() cuts each CJK run into overlapping pairs ("人工智能" becomes
"人工 工智 智能"), which needs no dictionary and no guess about the language, and
leaves the rest of the text as it is. The query side turns a CJK word into the
phrase of its pairs, so it matches exactly where the characters stand together.
A single character is left whole; the search looks for one-character queries in
the title instead. It wraps only the 'simple' half of the vector: through the
'english' half too, every pair would be indexed twice.

The character class matches CJK_CHARS in services/relevance_service.py (kana, CJK
ideographs, Hangul syllables and jamo). Text without any of them comes back
unchanged by the first test, so Latin articles pay next to nothing.

Measured on a copy with 168k articles: the index grew 18 % with realistic CJK
content (short bodies, as production has), the build took 106 s instead of 89 s,
and Latin searches did not change.

The index expression must stay identical to ``_FTS_VECTOR`` in
services/article.py, or the planner stops using the index and every search scans
the table. Built with the app down, like every migration here, so a plain build.

Revision ID: 0117
Revises: 0116
Create Date: 2026-10-02
"""
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None

_CJK = "぀-ヿㇰ-ㇿ㐀-䶿一-鿿豈-﫿가-힯ᄀ-ᇿ㄰-㆏"


def upgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION cjk_bigrams(t text) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
          SELECT CASE WHEN t !~ '[{_CJK}]' THEN t ELSE (
            SELECT string_agg(
              CASE WHEN m[1] IS NULL THEN m[2]
                   WHEN length(m[1]) = 1 THEN m[1]
                   ELSE (SELECT string_agg(substr(m[1], i, 2), ' ' ORDER BY i)
                         FROM generate_series(1, length(m[1]) - 1) i)
              END, ' ' ORDER BY n)
            FROM regexp_matches(t, '([{_CJK}]+)|([^{_CJK}]+)', 'g')
                 WITH ORDINALITY AS r(m, n))
          END
        $$
    """)
    op.execute("DROP INDEX IF EXISTS idx_articles_search_fts")
    op.execute("""
        CREATE INDEX idx_articles_search_fts ON articles
        USING GIN ((
            setweight(to_tsvector('simple', cjk_bigrams(immutable_unaccent(coalesce(title, '')))), 'A') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(title, ''))), 'A') ||
            setweight(to_tsvector('simple', cjk_bigrams(immutable_unaccent(coalesce(summary, '')))), 'B') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(summary, ''))), 'B') ||
            setweight(to_tsvector('simple', cjk_bigrams(immutable_unaccent(coalesce(content, '') || ' ' || coalesce(readable_content, '')))), 'D') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(content, '') || ' ' || coalesce(readable_content, ''))), 'D')
        ))
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_articles_search_fts")
    op.execute("""
        CREATE INDEX idx_articles_search_fts ON articles
        USING GIN ((
            setweight(to_tsvector('simple', immutable_unaccent(coalesce(title, ''))), 'A') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(title, ''))), 'A') ||
            setweight(to_tsvector('simple', immutable_unaccent(coalesce(summary, ''))), 'B') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(summary, ''))), 'B') ||
            setweight(to_tsvector('simple', immutable_unaccent(coalesce(content, '') || ' ' || coalesce(readable_content, ''))), 'D') ||
            setweight(to_tsvector('english', immutable_unaccent(coalesce(content, '') || ' ' || coalesce(readable_content, ''))), 'D')
        ))
    """)
    op.execute("DROP FUNCTION IF EXISTS cjk_bigrams(text)")

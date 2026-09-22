"""Integration tests for the lexical corpus statistics (build + cache).

Runs against the real (dev) database inside a transaction that is always rolled
back. The corpus tables are instance-wide, so there is nothing to scope the way
the purge tests scope a throwaway feed; the rollback is what protects the dev
data. Skips automatically if the database is unreachable.

Every test builds with ``window_days=1`` and stamps its articles ``now``, so the
window holds the test's own rows and the counts are exact rather than relative to
whatever the dev database fetched this month.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.relevance import LexicalCorpus, LexicalTerm
from app.services import relevance_corpus_service as rcs
from app.services import relevance_service as rs

NOW = datetime.now(timezone.utc)

# Invented tokens: nothing else in the window can contain them, so a count over
# them is a statement about this test's articles and not about the dev database.
COMMON = "qzlorp"   # in three articles, so it clears min_df=3
RARE = "vexmuq"     # in one, so it must not survive


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest.fixture(autouse=True)
def clear_cache():
    """The cache is a module global; a leftover from one test would mask another."""
    rcs.reset_cache()
    yield
    rcs.reset_cache()


async def _article(session, title, content=None, *, age_days=0) -> Article:
    u = uuid.uuid4().hex
    a = Article(feed_id=None, guid=u, guid_hash=u, title=title, content=content,
                fetched_at=NOW - timedelta(days=age_days))
    session.add(a)
    await session.flush()
    return a


async def _seed(session) -> None:
    await _article(session, f"{COMMON} alpha", "<p>body one</p>")
    await _article(session, f"{COMMON} beta", "<p>body two</p>")
    await _article(session, f"{COMMON} gamma", "<p>body three</p>")
    await _article(session, f"{RARE} delta", "<p>body four</p>")
    # Outside a one-day window: it must not reach the counts at all.
    await _article(session, f"{COMMON} ancient", "<p>old</p>", age_days=9)


@pytest.mark.asyncio
class TestRebuild:
    async def test_counts_the_window_and_drops_the_long_tail(self, pg):
        await _seed(pg)
        result = await rcs.rebuild(pg, window_days=1, min_df=3)

        assert result["n_docs"] == 4
        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == COMMON)) == 3
        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == RARE)) is None

    async def test_records_what_the_build_was_made_with(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)

        corpus = await pg.get(LexicalCorpus, 1)
        assert corpus.n_docs == 4
        assert corpus.avg_doc_len > 0
        assert (corpus.min_df, corpus.window_days) == (3, 1)
        assert corpus.ngram_max == rs.NGRAM_MAX
        assert corpus.built_at is not None

    async def test_is_idempotent(self, pg):
        await _seed(pg)
        first = await rcs.rebuild(pg, window_days=1, min_df=3)
        second = await rcs.rebuild(pg, window_days=1, min_df=3)

        assert (first["terms"], first["n_docs"]) == (second["terms"], second["n_docs"])
        assert first["avg_doc_len"] == second["avg_doc_len"]
        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == COMMON)) == 3

    async def test_html_is_stripped_before_counting(self, pg):
        await _article(pg, "one", "<p class='intro'>zorble text</p>")
        await _article(pg, "two", "<div class='intro'>zorble text</div>")
        await _article(pg, "three", "<span class='intro'>zorble text</span>")
        await rcs.rebuild(pg, window_days=1, min_df=3)

        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == "zorble")) == 3
        for markup in ("div", "span", "class", "intro"):
            assert await pg.scalar(
                select(LexicalTerm.doc_freq).where(LexicalTerm.term == markup)) is None

    async def test_empty_window_writes_an_empty_table(self, pg):
        result = await rcs.rebuild(pg, window_days=1, min_df=3)
        assert result["terms"] == 0
        assert await rcs.get_stats(pg) is None


@pytest.mark.asyncio
class TestGetStats:
    async def test_none_before_the_first_build(self, pg):
        assert await rcs.get_stats(pg) is None

    async def test_returns_the_built_table(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)

        stats = await rcs.get_stats(pg)
        assert stats.n_docs == 4
        assert stats.doc_freq[COMMON] == 3
        assert stats.avg_doc_len > 0

    async def test_reuses_the_cache_until_a_new_build(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)
        first = await rcs.get_stats(pg)
        assert await rcs.get_stats(pg) is first

        # A rebuild moves built_at, which is what the cache keys off.
        await _article(pg, f"{RARE} epsilon")
        await _article(pg, f"{RARE} zeta")
        await rcs.rebuild(pg, window_days=1, min_df=3)
        second = await rcs.get_stats(pg)
        assert second is not first
        assert second.doc_freq[RARE] == 3

    async def test_scores_an_article_against_the_stored_table(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)
        stats = await rcs.get_stats(pg)

        profile = rs.parse_profile(f"High relevance: {COMMON} topics")
        text = rs.article_text(f"{COMMON} alpha", "<p>body one</p>")
        assert rs.lexical_score(text, profile, stats) > 0.0
        assert rs.lexical_score("nothing in common here", profile, stats) == 0.0

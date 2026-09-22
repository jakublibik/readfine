"""Integration tests for the lexical corpus statistics (build + cache).

Runs against the real (dev) database inside a transaction that is always rolled
back. The corpus tables are instance-wide, so there is nothing to scope the way
the purge tests scope a throwaway feed; the rollback is what protects the dev
data. Skips automatically if the database is unreachable.

Every test builds with ``window_days=1`` and stamps its articles ``now``. That
window also holds whatever the dev database fetched today, so the assertions are
about invented tokens that nothing else can contain, never about the total.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
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

        assert result["n_docs"] >= 4
        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == COMMON)) == 3
        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == RARE)) is None

    async def test_records_what_the_build_was_made_with(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)

        corpus = await pg.get(LexicalCorpus, 1)
        assert corpus.n_docs >= 4
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
        """A window nothing falls into: the table is emptied, not left stale."""
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)
        assert await pg.scalar(
            select(LexicalTerm.doc_freq).where(LexicalTerm.term == COMMON)) == 3

        result = await rcs.rebuild(pg, window_days=0, min_df=3)
        assert result["terms"] == 0
        assert await rcs.get_stats(pg) is None


@pytest.mark.asyncio
class TestGetStats:
    async def test_none_before_the_first_build(self, pg):
        """A fresh install: no build, so no score, which is not a score of zero."""
        await pg.execute(delete(LexicalCorpus))
        assert await rcs.get_stats(pg) is None

    async def test_returns_the_built_table(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)

        stats = await rcs.get_stats(pg)
        assert stats.n_docs >= 4
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


@pytest.mark.asyncio
class TestEnsureBuilt:
    """The bootstrap path: a young instance must not wait for the nightly job."""

    async def test_builds_when_there_is_nothing_yet(self, pg):
        await pg.execute(delete(LexicalCorpus))
        await _seed(pg)

        assert await rcs.ensure_built(pg, window_days=1) is True
        assert await rcs.get_stats(pg) is not None

    async def test_keeps_rebuilding_while_the_corpus_is_tiny(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)
        corpus = await pg.get(LexicalCorpus, 1)
        corpus.n_docs = rcs.BOOTSTRAP_MAX_DOCS - 1
        first = corpus.built_at
        await pg.flush()

        assert await rcs.ensure_built(pg, window_days=1) is True
        assert (await pg.get(LexicalCorpus, 1)).built_at > first

    async def test_leaves_a_grown_corpus_to_the_nightly_job(self, pg):
        await _seed(pg)
        await rcs.rebuild(pg, window_days=1, min_df=3)
        corpus = await pg.get(LexicalCorpus, 1)
        corpus.n_docs = rcs.BOOTSTRAP_MAX_DOCS
        await pg.flush()

        assert await rcs.ensure_built(pg) is False


@pytest.mark.asyncio
class TestAutoGenerationCandidates:
    """Who the nightly profile regeneration considers.

    Lives with the relevance tests because the reason the rule changed is here:
    the interest profile now also feeds a scorer that runs without a model, so
    scheduling its regeneration stopped being a question about AI scoring.
    """

    async def _user(self, session, *, interval: int, ai_scoring: bool,
                    active_days: int = 0):
        from app.models.user import User, UserSettings
        u = uuid.uuid4().hex[:12]
        user = User(email=f"gen_{u}@test.invalid", password_hash="x",
                    display_name="t", is_active=True,
                    last_active_at=NOW - timedelta(days=active_days))
        session.add(user)
        await session.flush()
        session.add(UserSettings(user_id=user.id, ai_preference_auto_days=interval,
                                 ai_scoring_enabled_default=ai_scoring))
        await session.flush()
        return user

    async def test_ai_scoring_off_is_still_a_candidate(self, pg):
        from app.services.ai_profile_service import due_auto_generation_user_ids
        user = await self._user(pg, interval=14, ai_scoring=False)
        assert user.id in await due_auto_generation_user_ids(pg)

    async def test_no_interval_is_not_a_candidate(self, pg):
        from app.services.ai_profile_service import due_auto_generation_user_ids
        user = await self._user(pg, interval=0, ai_scoring=True)
        assert user.id not in await due_auto_generation_user_ids(pg)

    async def test_a_long_absence_drops_out(self, pg):
        from app.services.ai_profile_service import (
            AUTO_GENERATION_IDLE_DAYS,
            due_auto_generation_user_ids,
        )
        user = await self._user(pg, interval=14, ai_scoring=True,
                                active_days=AUTO_GENERATION_IDLE_DAYS + 1)
        assert user.id not in await due_auto_generation_user_ids(pg)

"""Integration tests for writing the lexical score at fetch time.

Same shape as the corpus tests: real (dev) database, one transaction, always
rolled back, skipped when the database is unreachable. Every article is stamped
``now`` and the corpus is built with ``window_days=1``, so the term statistics
are this test's own articles.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User, UserSettings
from app.services import lexical_score_service as lss
from app.services import relevance_corpus_service as rcs

NOW = datetime.now(timezone.utc)

TOPIC = "qzlorp"
PROFILE = f"High relevance: {TOPIC} research\nAvoid: vexmuq gossip"


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
    rcs.reset_cache()
    yield
    rcs.reset_cache()


async def _setup(session, *, profile: str | None = PROFILE,
                 enabled: bool = True) -> tuple[User, Feed]:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"lex_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, ai_preference_text=profile,
                             basic_scoring_enabled=enabled))
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return user, feed


async def _article(session, feed, title, content="<p>body</p>") -> Article:
    u = uuid.uuid4().hex
    a = Article(feed_id=feed.id, guid=u, guid_hash=u, title=title, content=content,
                fetched_at=NOW)
    session.add(a)
    await session.flush()
    return a


async def _corpus(session) -> None:
    """Enough articles sharing the topic word that it clears min_df."""
    for i in range(3):
        u = uuid.uuid4().hex
        session.add(Article(feed_id=None, guid=u, guid_hash=u,
                            title=f"{TOPIC} background {i}", content="<p>body</p>",
                            fetched_at=NOW - timedelta(minutes=5)))
    await session.flush()
    await rcs.rebuild(session, window_days=1, min_df=3)


async def _state(session, user, article) -> UserArticleState | None:
    return (await session.execute(
        select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article.id,
        )
    )).scalar_one_or_none()


@pytest.mark.asyncio
class TestScoreNewArticles:
    async def test_writes_a_score_for_a_matching_article(self, pg):
        user, feed = await _setup(pg)
        await _corpus(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")

        assert await lss.score_new_articles(pg, feed.id, [article]) == 1
        state = await _state(pg, user, article)
        assert 0.0 < state.lexical_score < 1.0
        assert state.ai_score is None

    async def test_no_overlap_is_stored_as_a_zero_not_as_nothing(self, pg):
        """0.0 and "never scored" have to stay distinguishable.

        A filter reading `score < 0.3` treats a missing row as NULL, so leaving
        these out would let the least relevant articles of all escape the very
        rule meant to sweep them.
        """
        user, feed = await _setup(pg)
        await _corpus(pg)
        article = await _article(pg, feed, "weather forecast for the weekend")

        assert await lss.score_new_articles(pg, feed.id, [article]) == 1
        assert (await _state(pg, user, article)).lexical_score == 0.0

    async def test_rewrites_a_score_the_previous_profile_left_behind(self, pg):
        user, feed = await _setup(pg)
        await _corpus(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")
        await lss.score_new_articles(pg, feed.id, [article])
        assert (await _state(pg, user, article)).lexical_score > 0

        settings = await pg.get(UserSettings, user.id)
        settings.ai_preference_text = "High relevance: entirely different subjects"
        await pg.flush()

        await lss.score_new_articles(pg, feed.id, [article])
        assert (await _state(pg, user, article)).lexical_score == 0.0

    async def test_reuses_an_existing_state_row(self, pg):
        user, feed = await _setup(pg)
        await _corpus(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")
        pg.add(UserArticleState(user_id=user.id, article_id=article.id, is_starred=True))
        await pg.flush()

        await lss.score_new_articles(pg, feed.id, [article])
        state = await _state(pg, user, article)
        assert state.lexical_score > 0
        assert state.is_starred is True

    async def test_the_avoid_list_cannot_lower_a_score(self, pg):
        """The plan's most expensive mistake, pinned end to end."""
        user, feed = await _setup(pg)
        await _corpus(pg)
        wanted = await _article(pg, feed, f"{TOPIC} findings published")
        both = await _article(pg, feed, f"{TOPIC} findings published vexmuq gossip")
        await lss.score_new_articles(pg, feed.id, [wanted, both])

        assert (await _state(pg, user, both)).lexical_score > 0


@pytest.mark.asyncio
class TestGates:
    async def test_skips_a_user_who_turned_it_off(self, pg):
        user, feed = await _setup(pg, enabled=False)
        await _corpus(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")

        assert await lss.score_new_articles(pg, feed.id, [article]) == 0
        assert await _state(pg, user, article) is None

    async def test_skips_a_user_without_a_profile(self, pg):
        """No profile means no row at all, which is the one meaning "no row" keeps."""
        user, feed = await _setup(pg, profile=None)
        await _corpus(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")

        assert await lss.score_new_articles(pg, feed.id, [article]) == 0
        assert await _state(pg, user, article) is None

    async def test_does_nothing_before_the_corpus_is_built(self, pg):
        user, feed = await _setup(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")

        assert await lss.score_new_articles(pg, feed.id, [article]) == 0
        assert await _state(pg, user, article) is None


async def _only_due_account(session, user_id: int) -> None:
    """Park every other account, so a due-count is about this test's user.

    The due query is instance-wide by design and the dev database has real
    accounts in it, several of which are due the moment the column exists. Rolled
    back with everything else.
    """
    await session.execute(
        text("UPDATE user_settings SET lexical_backfill_at = now() "
             "WHERE user_id <> :uid"),
        {"uid": user_id},
    )


@pytest.mark.asyncio
class TestBackfill:
    async def _due_setup(self, pg):
        user, feed = await _setup(pg)
        settings = await pg.get(UserSettings, user.id)
        settings.ai_preference_updated_at = NOW
        await pg.flush()
        await _only_due_account(pg, user.id)
        await _corpus(pg)
        return user, feed

    async def test_scores_recent_unread_articles(self, pg):
        user, feed = await self._due_setup(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")
        article.published_at = NOW - timedelta(days=2)
        await pg.flush()

        assert await lss.process_due_backfills(pg) == 1
        assert (await _state(pg, user, article)).lexical_score > 0

    async def test_leaves_articles_outside_the_window_alone(self, pg):
        user, feed = await self._due_setup(pg)
        old = await _article(pg, feed, f"{TOPIC} findings published")
        old.published_at = NOW - timedelta(days=lss.BACKFILL_DAYS + 1)
        await pg.flush()

        await lss.process_due_backfills(pg)
        assert await _state(pg, user, old) is None

    async def test_leaves_read_articles_alone(self, pg):
        user, feed = await self._due_setup(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")
        article.published_at = NOW - timedelta(days=1)
        pg.add(UserArticleState(user_id=user.id, article_id=article.id, is_read=True))
        await pg.flush()

        await lss.process_due_backfills(pg)
        assert (await _state(pg, user, article)).lexical_score is None

    async def test_stops_being_due_once_it_has_run(self, pg):
        user, feed = await self._due_setup(pg)
        article = await _article(pg, feed, f"{TOPIC} findings published")
        article.published_at = NOW - timedelta(days=1)
        await pg.flush()

        await lss.process_due_backfills(pg)
        settings = await pg.get(UserSettings, user.id)
        await pg.refresh(settings)
        assert settings.lexical_backfill_at is not None

        scored = (await _state(pg, user, article)).lexical_score
        # Running again must not be a second pass over the same account, and the
        # score it already wrote must not move.
        await lss.process_due_backfills(pg)
        assert (await _state(pg, user, article)).lexical_score == scored

    async def test_a_new_profile_makes_it_due_again(self, pg):
        user, feed = await self._due_setup(pg)
        await lss.process_due_backfills(pg)

        settings = await pg.get(UserSettings, user.id)
        settings.ai_preference_updated_at = NOW + timedelta(minutes=1)
        await pg.flush()

        article = await _article(pg, feed, f"{TOPIC} findings published")
        article.published_at = NOW - timedelta(days=1)
        await pg.flush()

        assert await lss.process_due_backfills(pg) == 1
        assert (await _state(pg, user, article)).lexical_score > 0

    async def test_an_account_that_never_saved_a_profile_is_not_due(self, pg):
        user, _feed = await _setup(pg, profile=None)
        await _only_due_account(pg, user.id)
        await _corpus(pg)
        assert await lss.process_due_backfills(pg) == 0

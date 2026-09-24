"""Integration tests for when score filters run: at fetch, after AI scoring, or in
the fallback over the basic score.

Same shape as the lexical scoring tests: real (dev) database, one transaction,
always rolled back, skipped when the database is unreachable. The basic score is
written straight into the state row, as score_new_articles would have just before
the filters run, so no corpus is needed.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload

from app.config import settings as app_settings
from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import ArticleLabel, Label
from app.models.user import User, UserSettings
from app.services import filter_service as fs

NOW = datetime.now(timezone.utc)


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
        # The fallback sweep is instance-wide; park whatever the dev database has.
        await session.execute(
            update(UserArticleState)
            .where(UserArticleState.relevance_filters_pending == True)  # noqa: E712
            .values(relevance_filters_pending=False))
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _setup(session) -> tuple[User, Feed, UserFeed]:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"sf_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, relevance_terms="cycling"))
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    # No readable extraction, so a label goes straight to AI scoring.
    uf = UserFeed(user_id=user.id, feed_id=feed.id, extract_readable=False)
    session.add(uf)
    await session.flush()
    return user, feed, uf


async def _article(session, feed, user, *, basic: float | None,
                   readable_status: str = "skipped") -> Article:
    u = uuid.uuid4().hex
    a = Article(feed_id=feed.id, guid=u, guid_hash=u, title="t", content="<p>b</p>",
                fetched_at=NOW, readable_status=readable_status)
    session.add(a)
    await session.flush()
    if basic is not None:
        session.add(UserArticleState(user_id=user.id, article_id=a.id, lexical_score=basic))
        await session.flush()
    return a


async def _filter(session, user, field, op, value, action="star", *,
                  action_value=None, position=0) -> Filter:
    f = Filter(user_id=user.id, name=f"{field} {op} {value}", position=position)
    session.add(f)
    await session.flush()
    if field:
        session.add(FilterCondition(filter_id=f.id, field=field, operator=op, value=value))
    session.add(FilterAction(filter_id=f.id, action_type=action, action_value=action_value))
    await session.flush()
    return f


async def _label_everything(session, user) -> None:
    """A regular filter that labels every article, the usual way into AI scoring."""
    label = Label(user_id=user.id, name="all")
    session.add(label)
    await session.flush()
    await _filter(session, user, "title", "contains", "t", "label",
                  action_value=str(label.id), position=0)


async def _state(session, user, article) -> UserArticleState | None:
    return (await session.execute(
        select(UserArticleState)
        .where(UserArticleState.user_id == user.id,
               UserArticleState.article_id == article.id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


async def _filters(session, user) -> list[Filter]:
    return list((await session.execute(
        select(Filter).where(Filter.user_id == user.id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(*fs.FILTER_ORDER)
    )).scalars())


def _ai_scoring(enqueued: bool):
    return patch("app.services.ai_scoring_service.enqueue_scoring_job",
                 AsyncMock(return_value=enqueued))


@pytest.mark.asyncio
class TestAtFetch:
    async def test_basic_filter_runs_with_the_regular_ones(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "basic_score", "gt", "50")
        high = await _article(pg, feed, user, basic=0.8)
        low = await _article(pg, feed, user, basic=0.2)

        await fs.apply_filters_to_new_articles(feed.id, [high, low], pg)

        assert (await _state(pg, user, high)).is_starred is True
        assert (await _state(pg, user, low)).is_starred is False

    async def test_basic_filter_without_terms_never_matches(self, pg):
        """No terms, no row: `basic < 30` must not sweep everything."""
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "basic_score", "lt", "30", "mark_read")
        article = await _article(pg, feed, user, basic=None)

        await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        state = await _state(pg, user, article)
        assert state is None or state.is_read is False

    async def test_relevance_filter_uses_basic_when_no_ai_score_is_coming(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)

        await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        state = await _state(pg, user, article)
        assert state.is_starred is True
        assert state.relevance_filters_pending is False

    async def test_relevance_filter_waits_when_a_label_sent_it_to_ai(self, pg):
        user, feed, _ = await _setup(pg)
        await _label_everything(pg, user)
        await _filter(pg, user, "relevance_score", "gt", "50", position=1)
        article = await _article(pg, feed, user, basic=0.8)

        with _ai_scoring(enqueued=True):
            await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        state = await _state(pg, user, article)
        assert state.is_starred is False
        assert state.relevance_filters_pending is True

    async def test_on_a_readable_feed_it_waits_for_the_extraction_and_scoring(self, pg):
        user, feed, uf = await _setup(pg)
        uf.extract_readable = True
        await _label_everything(pg, user)
        await _filter(pg, user, "relevance_score", "gt", "50", position=1)
        article = await _article(pg, feed, user, basic=0.8)

        with patch("app.services.filter_service._scoring_would_run",
                   AsyncMock(return_value=True)):
            await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        state = await _state(pg, user, article)
        assert article.readable_status == "pending"
        assert state.is_starred is False
        assert state.relevance_filters_pending is True

    async def test_relevance_filter_does_not_wait_when_scoring_is_off(self, pg):
        """A label with no AI scoring behind it is just a label."""
        user, feed, _ = await _setup(pg)
        await _label_everything(pg, user)
        await _filter(pg, user, "relevance_score", "gt", "50", position=1)
        article = await _article(pg, feed, user, basic=0.8)

        with _ai_scoring(enqueued=False):
            await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        assert (await _state(pg, user, article)).is_starred is True

    async def test_a_label_from_a_relevance_filter_goes_to_ai_scoring(self, pg):
        """Same rule as a label from any fetch filter: labeled means AI-scored.

        The filter itself is done, so the AI score arriving later must not run it
        again: nothing is parked.
        """
        user, feed, _ = await _setup(pg)
        label = Label(user_id=user.id, name="top")
        pg.add(label)
        await pg.flush()
        await _filter(pg, user, "relevance_score", "gt", "50", "label",
                      action_value=str(label.id))
        article = await _article(pg, feed, user, basic=0.8)

        with _ai_scoring(enqueued=True) as enqueue:
            await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        enqueue.assert_awaited_once()
        assert (await _state(pg, user, article)).relevance_filters_pending is False

    async def test_a_star_from_a_relevance_filter_queues_the_extraction(self, pg):
        user, feed, uf = await _setup(pg)
        uf.extract_readable = True
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)

        with _ai_scoring(enqueued=False) as enqueue:
            await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        assert article.readable_status == "pending"
        enqueue.assert_not_awaited()

    async def test_parking_creates_the_state_row_it_needs(self, pg):
        user, feed, _ = await _setup(pg)
        await _label_everything(pg, user)
        await _filter(pg, user, "relevance_score", "gt", "50", position=1)
        article = await _article(pg, feed, user, basic=None)

        with _ai_scoring(enqueued=True):
            await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        assert (await _state(pg, user, article)).relevance_filters_pending is True

    async def test_ai_filter_is_left_for_the_ai_pass(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "ai_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)

        await fs.apply_filters_to_new_articles(feed.id, [article], pg)

        assert (await _state(pg, user, article)).is_starred is False


@pytest.mark.asyncio
class TestAiPass:
    async def test_parked_relevance_filter_reads_the_ai_score(self, pg):
        user, feed, uf = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)
        state = await _state(pg, user, article)
        state.relevance_filters_pending = True
        state.ai_score = 0.2  # the AI disagrees with the words

        await fs._apply_ai_filters_for_state(state, article, uf, await _filters(pg, user), pg)

        assert state.is_starred is False
        assert state.relevance_filters_pending is False
        assert state.ai_filters_applied is True

    async def test_relevance_filter_that_already_ran_is_not_run_again(self, pg):
        """An AI score arriving later (a label added by hand) must not re-run it."""
        user, feed, uf = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.1)
        state = await _state(pg, user, article)
        state.ai_score = 0.9

        await fs._apply_ai_filters_for_state(state, article, uf, await _filters(pg, user), pg)

        assert state.is_starred is False

    async def test_ai_filter_still_runs_on_every_fresh_score(self, pg):
        user, feed, uf = await _setup(pg)
        await _filter(pg, user, "ai_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.1)
        state = await _state(pg, user, article)
        state.ai_score = 0.9

        await fs._apply_ai_filters_for_state(state, article, uf, await _filters(pg, user), pg)

        assert state.is_starred is True


async def _park(session, user, article, *, job_status: str | None) -> UserArticleState:
    state = await _state(session, user, article)
    state.relevance_filters_pending = True
    if job_status is not None:
        session.add(ArticleAiJob(article_id=article.id, user_id=user.id,
                                 operation="scoring", status=job_status))
    await session.flush()
    return state


def _ai_on(on: bool = True):
    return patch("app.services.ai_jobs.ai_enabled_globally", AsyncMock(return_value=on))


@pytest.mark.asyncio
class TestFallback:
    async def test_failed_scoring_falls_back_to_basic(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)
        await _park(pg, user, article, job_status="failed")

        with _ai_on():
            assert await fs.process_relevance_fallback(pg) == 1

        state = await _state(pg, user, article)
        assert state.is_starred is True
        assert state.relevance_filters_pending is False

    async def test_waits_while_the_scoring_job_is_pending(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)
        await _park(pg, user, article, job_status="pending")

        with _ai_on():
            assert await fs.process_relevance_fallback(pg) == 0

        assert (await _state(pg, user, article)).relevance_filters_pending is True

    async def test_a_pending_job_does_not_hold_it_when_ai_is_off_instance_wide(self, pg):
        """Nothing processes the queue then, so waiting would be forever."""
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)
        await _park(pg, user, article, job_status="pending")

        with _ai_on(False):
            assert await fs.process_relevance_fallback(pg) == 1

        assert (await _state(pg, user, article)).is_starred is True

    async def test_waits_for_a_readable_extraction_in_progress(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8, readable_status="pending")
        await _park(pg, user, article, job_status=None)

        with _ai_on():
            assert await fs.process_relevance_fallback(pg) == 0

    async def test_runs_each_parked_article_once(self, pg):
        user, feed, _ = await _setup(pg)
        await _filter(pg, user, "relevance_score", "gt", "50")
        article = await _article(pg, feed, user, basic=0.8)
        await _park(pg, user, article, job_status="skipped")

        with _ai_on():
            assert await fs.process_relevance_fallback(pg) == 1
            assert await fs.process_relevance_fallback(pg) == 0

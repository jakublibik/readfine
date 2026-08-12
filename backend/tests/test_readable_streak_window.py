"""Integration tests: the auto-disable streaks only look at articles since re-enable.

Disabling readable extraction leaves the articles that caused it alone, so the window
the cross-batch checks read has to start at the last re-enable. Without that bound the
old 403s stay a feed's newest terminal rows and one fresh failure re-disables it, which
turns a threshold of 3 (and 5 for empty extractions) into 1.

Against real Postgres rather than a mocked session, because the whole bug lives in the
shape of that window: which rows the query returns, in what order, relative to a
watermark.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed
from app.services.readable_service import (
    _CONSECUTIVE_403_THRESHOLD,
    _CONSECUTIVE_EMPTY_THRESHOLD,
    _EMPTY_CONTENT_MSG,
    _recent_terminal_articles,
    stamp_readable_streak_start,
)

pytestmark = pytest.mark.asyncio

_403 = "HTTP 403 Forbidden"


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
        await conn.close()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    created: list[int] = []
    session.info["created_feed_ids"] = created
    try:
        yield session
    finally:
        await session.rollback()
        if created:
            await session.execute(delete(Article).where(Article.feed_id.in_(created)))
            await session.execute(delete(Feed).where(Feed.id.in_(created)))
        await session.commit()
        await session.close()
        await engine.dispose()


async def _feed(session) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://example.com/{u}.xml", title=f"f-{u[:6]}", subscriber_count=1)
    session.add(feed)
    await session.commit()
    session.info["created_feed_ids"].append(feed.id)
    return feed


async def _article(session, feed: Feed, status: str, error: str | None = None) -> Article:
    """One terminal extraction outcome, appended after everything already there."""
    u = uuid.uuid4().hex
    article = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=f"a-{u[:6]}",
        url=f"https://example.com/{u}", readable_status=status, readable_error=error,
    )
    session.add(article)
    await session.commit()
    return article


async def _403s(session, feed: Feed, n: int) -> None:
    for _ in range(n):
        await _article(session, feed, "failed", _403)


class TestStreakWindow:
    async def test_counts_the_whole_history_when_never_re_enabled(self, pg):
        """NULL watermark keeps the old behaviour, which is what every migrated row has."""
        feed = await _feed(pg)
        await _403s(pg, feed, _CONSECUTIVE_403_THRESHOLD)

        rows = await _recent_terminal_articles(feed.id, _CONSECUTIVE_403_THRESHOLD, pg)

        assert feed.readable_streak_from_id is None
        assert len(rows) == _CONSECUTIVE_403_THRESHOLD

    async def test_failures_from_before_re_enable_are_not_counted_again(self, pg):
        """The bug itself: one fresh 403 after a re-enable used to re-disable the feed."""
        feed = await _feed(pg)
        await _403s(pg, feed, _CONSECUTIVE_403_THRESHOLD)

        await stamp_readable_streak_start(feed.id, pg)
        await pg.commit()
        await _403s(pg, feed, 1)

        rows = await _recent_terminal_articles(feed.id, _CONSECUTIVE_403_THRESHOLD, pg)

        assert len(rows) == 1  # below the threshold, so no disable

    async def test_a_full_streak_after_re_enable_still_counts(self, pg):
        """A feed that is genuinely still blocked must go off again, just not at once."""
        feed = await _feed(pg)
        await _403s(pg, feed, _CONSECUTIVE_403_THRESHOLD)

        await stamp_readable_streak_start(feed.id, pg)
        await pg.commit()
        await _403s(pg, feed, _CONSECUTIVE_403_THRESHOLD)

        rows = await _recent_terminal_articles(feed.id, _CONSECUTIVE_403_THRESHOLD, pg)

        assert len(rows) == _CONSECUTIVE_403_THRESHOLD
        assert all(status == "failed" and error == _403 for status, error in rows)

    async def test_a_success_inside_the_window_breaks_the_streak(self, pg):
        """Successes have to stay in the window; they carry no timestamp of their own,
        which is why the watermark is an article id and not a point in time."""
        feed = await _feed(pg)
        await stamp_readable_streak_start(feed.id, pg)
        await pg.commit()

        await _403s(pg, feed, 1)
        await _article(pg, feed, "success")
        await _403s(pg, feed, _CONSECUTIVE_403_THRESHOLD - 1)

        rows = await _recent_terminal_articles(feed.id, _CONSECUTIVE_403_THRESHOLD, pg)

        assert len(rows) == _CONSECUTIVE_403_THRESHOLD
        assert ("success", None) in rows

    async def test_empty_extraction_streak_uses_the_same_window(self, pg):
        """The empty-extraction twin has no revival probe, so a manual re-enable is the
        only way back and it is exactly the path that used to re-trip on one article."""
        feed = await _feed(pg)
        for _ in range(_CONSECUTIVE_EMPTY_THRESHOLD):
            await _article(pg, feed, "failed", _EMPTY_CONTENT_MSG)

        await stamp_readable_streak_start(feed.id, pg)
        await pg.commit()
        await _article(pg, feed, "failed", _EMPTY_CONTENT_MSG)

        rows = await _recent_terminal_articles(feed.id, _CONSECUTIVE_EMPTY_THRESHOLD, pg)

        assert len(rows) == 1

    async def test_stamping_a_feed_with_no_articles_leaves_the_window_open(self, pg):
        """Nothing to point at yet — the next articles are all fair game."""
        feed = await _feed(pg)

        await stamp_readable_streak_start(feed.id, pg)
        await pg.commit()
        await pg.refresh(feed)

        assert feed.readable_streak_from_id is None
        await _403s(pg, feed, _CONSECUTIVE_403_THRESHOLD)
        rows = await _recent_terminal_articles(feed.id, _CONSECUTIVE_403_THRESHOLD, pg)
        assert len(rows) == _CONSECUTIVE_403_THRESHOLD

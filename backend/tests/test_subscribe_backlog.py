"""Subscribing to a feed the instance already has does not hand over its whole history
as unread.

A shared feed keeps what it fetched up to the retention horizon, about 900 articles
for The Verge, where the same feed added fresh brings the 15 in its RSS. The new
subscriber gets the last week and at least the newest ten unread; the rest is marked
read as 'backlog', which is no one's reading.

Runs against the real (dev) DB in a rolled-back transaction and skips if unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import (
    SUPPRESSED_BY_BACKLOG, USER_READ_SUPPRESSED_BY, Article, UserArticleState,
)
from app.models.feed import Feed
from app.models.user import User
from app.services import feed as feed_service
from app.services.stats_service import get_intake_stats

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg(monkeypatch):
    async def no_check(url):
        return None

    monkeypatch.setattr(feed_service, "async_validate_feed_url", no_check)
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _user(pg):
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}")
    pg.add(user)
    await pg.flush()
    return user


async def _shared_feed(pg, *days_ago, trimmed=()):
    """A feed someone else reads, with an article published this many days ago each."""
    feed = Feed(feed_url=f"https://ex.invalid/{uuid.uuid4().hex}.xml", title="shared",
                subscriber_count=1)
    pg.add(feed)
    await pg.flush()
    articles = []
    for d in days_ago:
        g = uuid.uuid4().hex
        when = NOW - timedelta(days=d)
        a = Article(feed_id=feed.id, guid=g, guid_hash=g, title=f"{d} days", content="x",
                    published_at=when, fetched_at=when,
                    trimmed_at=NOW if d in trimmed else None)
        pg.add(a)
        articles.append(a)
    await pg.flush()
    return feed, articles


async def _subscribe(pg, user, feed):
    return await feed_service.subscribe(
        user=user, url=feed.feed_url, folder_id=None, custom_title=None,
        fetch_auth_user=None, fetch_auth_pass=None, db=pg, trigger_initial_fetch=False,
    )


async def _backlog_ids(pg, user):
    return set((await pg.scalars(select(UserArticleState.article_id).where(
        UserArticleState.user_id == user.id,
        UserArticleState.suppressed_by == SUPPRESSED_BY_BACKLOG,
        UserArticleState.is_read.is_(True),
    ))).all())


async def test_the_last_week_and_the_newest_ten_stay_unread(pg):
    # 3 from this week, 12 older: the week's 3 and the 7 newest older ones stay.
    feed, articles = await _shared_feed(pg, 1, 2, 3, *range(10, 22))
    user = await _user(pg)
    await _subscribe(pg, user, feed)
    assert await _backlog_ids(pg, user) == {a.id for a in articles[10:]}


async def test_a_busy_feed_keeps_the_whole_week(pg):
    feed, articles = await _shared_feed(pg, *[0.5] * 12, 8, 9)
    user = await _user(pg)
    await _subscribe(pg, user, feed)
    assert await _backlog_ids(pg, user) == {articles[12].id, articles[13].id}


async def test_a_slow_feed_keeps_its_last_posts(pg):
    feed, _ = await _shared_feed(pg, 20, 45, 70, 90)
    user = await _user(pg)
    await _subscribe(pg, user, feed)
    assert await _backlog_ids(pg, user) == set()


async def test_a_new_feed_is_left_alone(pg):
    # Nothing to mark: a feed that did not exist has no articles before the reader.
    user = await _user(pg)
    url = f"https://ex.invalid/{uuid.uuid4().hex}.xml"

    async def fake_fetch(*a, **kw):
        import feedparser
        return feedparser.parse(
            '<?xml version="1.0"?><rss version="2.0"><channel><title>n</title>'
            '</channel></rss>'), url

    orig = feed_service.fetch_and_parse_url
    feed_service.fetch_and_parse_url = fake_fetch
    try:
        await feed_service.subscribe(
            user=user, url=url, folder_id=None, custom_title=None,
            fetch_auth_user=None, fetch_auth_pass=None, db=pg, trigger_initial_fetch=False,
        )
    finally:
        feed_service.fetch_and_parse_url = orig
    assert await _backlog_ids(pg, user) == set()


async def test_a_returning_reader_keeps_their_own_state(pg):
    feed, articles = await _shared_feed(pg, *range(10, 25))
    user = await _user(pg)
    kept = articles[-1]  # the oldest, which would be backlog
    pg.add(UserArticleState(user_id=user.id, article_id=kept.id, is_read=False,
                            is_starred=True, user_starred=True, ever_starred=True))
    await pg.flush()
    await _subscribe(pg, user, feed)
    state = await pg.get(UserArticleState, (user.id, kept.id))
    await pg.refresh(state)
    assert state.is_starred and not state.is_read and state.suppressed_by is None
    assert kept.id not in await _backlog_ids(pg, user)


async def test_trimmed_articles_are_not_written(pg):
    feed, articles = await _shared_feed(pg, *range(10, 22), trimmed=(21,))
    user = await _user(pg)
    await _subscribe(pg, user, feed)
    assert articles[-1].id not in await _backlog_ids(pg, user)


async def test_backlog_is_not_reading_and_not_intake(pg):
    assert SUPPRESSED_BY_BACKLOG not in USER_READ_SUPPRESSED_BY
    feed, _ = await _shared_feed(pg, 1, *range(10, 22))
    user = await _user(pg)
    await _subscribe(pg, user, feed)
    stats = await get_intake_stats(user.id, pg, collapsing=False)
    # 13 fetched in the month, the 3 oldest came before the reader: 10 came at them.
    assert stats.all.fetched == 10

"""Active readers on the admin dashboard: who read something themselves, and when.

Runs against the real (dev) DB in a rolled-back transaction and skips if unreachable.
The count is instance-wide, so every test counts from a `now` far in the future, where
only the rows it wrote itself fall inside the windows.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import (
    SUPPRESSED_BY_BULK,
    SUPPRESSED_BY_FILTER,
    SUPPRESSED_BY_SIMILAR,
    SUPPRESSED_BY_STORY,
    SUPPRESSED_BY_URL,
    Article,
    UserArticleState,
)
from app.models.feed import Feed
from app.models.user import User
from app.services.admin_service import count_active_readers

NOW = datetime(2099, 6, 1, tzinfo=timezone.utc)


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
    session = AsyncSession(bind=conn, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def feed(pg):
    u = uuid.uuid4().hex
    f = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"feed-{u[:6]}", subscriber_count=1)
    pg.add(f)
    await pg.flush()
    return f


async def _reader(pg, feed, *reads):
    """A user with one read article per (days_ago, suppressed_by) pair."""
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}")
    pg.add(user)
    await pg.flush()
    for days_ago, by in reads:
        g = uuid.uuid4().hex
        a = Article(feed_id=feed.id, guid=g, guid_hash=g, title="x", content="<p>x</p>",
                    readable_status="success", published_at=NOW, fetched_at=NOW)
        pg.add(a)
        await pg.flush()
        pg.add(UserArticleState(user_id=user.id, article_id=a.id, is_read=True,
                                read_at=NOW - timedelta(days=days_ago), suppressed_by=by))
    await pg.flush()
    return user


async def test_own_reads_count_in_their_window(pg, feed):
    await _reader(pg, feed, (1, None))                # read by hand this week
    await _reader(pg, feed, (2, SUPPRESSED_BY_BULK))  # mark all read this week
    await _reader(pg, feed, (20, None))               # only inside 30 days
    await _reader(pg, feed, (45, None))               # outside both
    assert await count_active_readers(pg, NOW) == {7: 2, 30: 3}


async def test_automatic_reads_do_not_count(pg, feed):
    for by in (SUPPRESSED_BY_FILTER, SUPPRESSED_BY_SIMILAR, SUPPRESSED_BY_URL,
               SUPPRESSED_BY_STORY):
        await _reader(pg, feed, (1, by))
    assert await count_active_readers(pg, NOW) == {7: 0, 30: 0}


async def test_a_reader_counts_once(pg, feed):
    await _reader(pg, feed, (1, None), (3, None), (3, SUPPRESSED_BY_FILTER), (15, None))
    assert await count_active_readers(pg, NOW) == {7: 1, 30: 1}

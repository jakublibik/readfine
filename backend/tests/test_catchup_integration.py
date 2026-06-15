"""Integration test: catch-up/briefing must exclude retention-trimmed stubs (#18).

Runs against the real (dev) database inside a rolled-back transaction. Skips
automatically if the database is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.catchup_service import fetch_catchup_articles

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception:
        await engine.dispose()
        pytest.skip("database not reachable")
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _setup(session):
    u = uuid.uuid4().hex[:12]
    user = User(email=f"catchup_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="Feed", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return user, feed


async def _article(session, feed, *, title, trimmed_at=None):
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=title,
        content="<p>body</p>", readable_status="success",
        published_at=NOW - timedelta(hours=1), fetched_at=NOW - timedelta(hours=1),
        trimmed_at=trimmed_at,
    )
    session.add(a)
    await session.flush()
    return a


async def test_trimmed_articles_excluded_from_catchup(pg):
    user, feed = await _setup(pg)
    normal = await _article(pg, feed, title="Normal")
    await _article(pg, feed, title="Trimmed", trimmed_at=NOW - timedelta(minutes=30))

    results = await fetch_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg,
        period="7days", scope_include=None,
        filter_status="all", filter_labeled=False, filter_score_min=None,
    )

    titles = {r.title for r in results}
    ids = {r.id for r in results}
    assert normal.id in ids
    assert "Trimmed" not in titles

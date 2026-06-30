"""Search sort + status filter for the article listing.

`list_articles(q=..., sort_order=..., read_status=...)` powers the search modal:
- sort_order "newest"/"oldest" overrides the default relevance (ts_rank) ordering;
- read_status "unread"/"read" restricts by read state ("all"/None = no filter).

Each test uses a unique nonsense token in the article titles so the full-text
match only ever hits the rows created here, never other dev-DB articles.

Runs against the real (dev) database inside a transaction that is always rolled
back. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.article import list_articles

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
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _art(session, feed, *, title, published_at) -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=title,
        content="<p>body</p>", readable_status="pending",
        published_at=published_at, fetched_at=published_at,
    )
    session.add(a)
    await session.flush()
    return a


async def _setup(session):
    """One user/feed; two matching articles: an older unread + a newer read one."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"search_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()

    token = "zsearchtok" + uuid.uuid4().hex[:10]
    older = await _art(session, feed, title=f"{token} alpha", published_at=NOW - timedelta(hours=2))
    newer = await _art(session, feed, title=f"{token} beta", published_at=NOW - timedelta(hours=1))
    # Mark the newer one read.
    session.add(UserArticleState(user_id=user.id, article_id=newer.id, is_read=True))
    await session.flush()
    return user, token, older, newer


async def _search(session, user, token, **kw):
    return await list_articles(user=user, db=session, q=token, limit=100, **kw)


async def test_status_all_returns_both(pg):
    user, token, older, newer = await _setup(pg)
    ids = {i.id for i in await _search(pg, user, token)}
    assert ids == {older.id, newer.id}


async def test_status_unread_excludes_read(pg):
    user, token, older, newer = await _setup(pg)
    ids = {i.id for i in await _search(pg, user, token, read_status="unread")}
    assert ids == {older.id}


async def test_status_read_only(pg):
    user, token, older, newer = await _setup(pg)
    ids = {i.id for i in await _search(pg, user, token, read_status="read")}
    assert ids == {newer.id}


async def test_sort_newest_first(pg):
    user, token, older, newer = await _setup(pg)
    ids = [i.id for i in await _search(pg, user, token, sort_order="newest")]
    assert ids == [newer.id, older.id]


async def test_sort_oldest_first(pg):
    user, token, older, newer = await _setup(pg)
    ids = [i.id for i in await _search(pg, user, token, sort_order="oldest")]
    assert ids == [older.id, newer.id]

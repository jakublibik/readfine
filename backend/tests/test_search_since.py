"""Time window in search.

`list_articles(since_days=N)` powers the Published row of the search modal: only
articles from the last N days, measured on the date the list sorts by (published,
else fetched), counted back from now.

Runs against the real (dev) database inside a transaction that is always rolled
back. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.routers.web.app.articles import _build_filter_params, search_filter_count, story_scope
from app.services.article import count_articles, list_articles

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


async def _setup(session):
    """a: published 2 hours ago | b: published 2 days ago | c: published 10 days
    ago | d: no publish date, fetched 5 hours ago | e: dated a year back but fetched
    today (the window goes by the publish date, so e is old)."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"since_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()

    token = "zsincetok" + uuid.uuid4().hex[:10]
    arts = {}
    for name, published, fetched in [
        ("a", NOW - timedelta(hours=2), NOW - timedelta(hours=2)),
        ("b", NOW - timedelta(days=2), NOW - timedelta(days=2)),
        ("c", NOW - timedelta(days=10), NOW - timedelta(days=10)),
        ("d", None, NOW - timedelta(hours=5)),
        ("e", NOW - timedelta(days=365), NOW - timedelta(hours=1)),
    ]:
        g = uuid.uuid4().hex
        a = Article(
            feed_id=feed.id, guid=g, guid_hash=g, title=f"{token} {name}",
            content="<p>body</p>", readable_status="pending",
            published_at=published, fetched_at=fetched,
        )
        session.add(a)
        await session.flush()
        session.add(UserArticleState(user_id=user.id, article_id=a.id))
        arts[name] = a
    await session.flush()
    return user, feed, token, arts


def _names(items, arts):
    by_id = {a.id: n for n, a in arts.items()}
    return sorted(by_id[i.id] for i in items)


async def test_window_goes_by_publish_date_else_fetch_date(pg):
    user, feed, token, arts = await _setup(pg)
    day = await list_articles(user=user, db=pg, q=token, sort_order="newest", since_days=1)
    week = await list_articles(user=user, db=pg, feed_id=feed.id, since_days=7)
    assert _names(day, arts) == ["a", "d"]
    assert _names(week, arts) == ["a", "b", "d"]


async def test_no_window_keeps_everything(pg):
    user, feed, token, arts = await _setup(pg)
    items = await list_articles(user=user, db=pg, q=token, sort_order="newest")
    assert _names(items, arts) == ["a", "b", "c", "d", "e"]


async def test_count_matches_the_list(pg):
    user, feed, token, arts = await _setup(pg)
    assert await count_articles(user, pg, collapsing=False, q=token, since_days=7) == 3


def test_window_travels_with_paging_and_unfolding():
    params = _build_filter_params(
        feed_id=None, folder_id=None, scope_include=None, label_id=None,
        unread_only=False, starred_only=False, archived_only=False, saved_only=False,
        labeled_only=False, q=None, is_search=True, sort_order="newest",
        read_status=None, label_filter=None, score={}, since_days=7,
    )
    assert params["since_days"] == 7
    assert story_scope(since_days=7) == {"since_days": 7}
    assert story_scope() == {}


def test_filter_count():
    none = dict(read_status=None, scope_include=None, label_filter=None, score={}, since_days=None)
    assert search_filter_count(**none) == 0
    # "all" status, an empty scope list and a score sort without a condition are no filters.
    assert search_filter_count(**{**none, "read_status": "all", "scope_include": "[]",
                                  "score": {"score_source": "ai"}}) == 0
    assert search_filter_count(
        read_status="unread", scope_include='["folder:2"]', label_filter='["any"]',
        score={"score_source": "ai", "score_op": "gte", "score_val": 70}, since_days=7,
    ) == 5


def test_list_filter_is_normalized_and_counted():
    from app.routers.web.app.articles import search_state
    assert search_state("starred") == "starred"
    assert search_state("bogus") is None
    assert search_state(None) is None
    none = dict(read_status=None, scope_include=None, label_filter=None, score={}, since_days=None)
    assert search_filter_count(**none, state="saved") == 1
    assert search_filter_count(**none, state="bogus") == 0

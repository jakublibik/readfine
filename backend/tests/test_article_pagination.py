"""Keyset pagination for the article-list infinite scroll.

Regression coverage for the bug where offset pagination + unread_only +
mark-read-on-scroll skipped articles: as rows are marked read mid-scroll they
leave the unread set, so a numeric offset for the next batch overshoots. Keyset
pagination on (sort_ts, id) is immune to that.

Runs against the real (dev) database inside a transaction that is always rolled
back; scoped to a throwaway feed. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.routers.web.app import _build_more_qs
from app.schemas.article import ArticleListItem
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


async def _setup(session) -> tuple[User, Feed]:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"page_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return user, feed


async def _art(session, feed, *, published_at) -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title="T",
        content="<p>body</p>", readable_status="pending",
        published_at=published_at, fetched_at=published_at,
    )
    session.add(a)
    await session.flush()
    return a


async def _mark_read(session, user, article) -> None:
    session.add(UserArticleState(
        user_id=user.id, article_id=article.id, is_read=True,
    ))
    await session.flush()


def _cursor(item: ArticleListItem) -> dict:
    return {"cursor_ts": item.sort_ts, "cursor_id": item.id}


class TestKeysetPagination:
    async def test_mark_read_between_batches_skips_nothing(self, pg):
        """Original bug: read the first page mid-scroll, the next keyset batch
        must still cover every remaining unread article."""
        user, feed = await _setup(pg)
        # 5 unread, strictly descending published_at (a[0] newest .. a[4] oldest)
        arts = [await _art(pg, feed, published_at=NOW - timedelta(hours=i)) for i in range(5)]

        page1 = await list_articles(user, pg, feed_id=feed.id, unread_only=True, limit=2)
        assert [it.id for it in page1] == [arts[0].id, arts[1].id]

        # Reading the whole first page shrinks the unread set — the trap for offset.
        await _mark_read(pg, user, arts[0])
        await _mark_read(pg, user, arts[1])

        page2 = await list_articles(
            user, pg, feed_id=feed.id, unread_only=True, limit=2, **_cursor(page1[-1])
        )
        page3 = await list_articles(
            user, pg, feed_id=feed.id, unread_only=True, limit=2, **_cursor(page2[-1])
        )

        seen = [it.id for it in page1 + page2 + page3]
        # No duplicates, and every article surfaced exactly once.
        assert len(seen) == len(set(seen))
        assert set(seen) == {a.id for a in arts}

    async def test_tiebreaker_stable_on_equal_published_at(self, pg):
        """Articles sharing published_at must page deterministically via the id
        tiebreaker — no duplicates, no skips."""
        user, feed = await _setup(pg)
        ts = NOW - timedelta(hours=1)
        arts = [await _art(pg, feed, published_at=ts) for _ in range(3)]

        page1 = await list_articles(user, pg, feed_id=feed.id, limit=2)
        page2 = await list_articles(user, pg, feed_id=feed.id, limit=2, **_cursor(page1[-1]))

        ids = [it.id for it in page1] + [it.id for it in page2]
        assert len(ids) == 3
        assert len(set(ids)) == 3
        assert set(ids) == {a.id for a in arts}
        # newest sort with equal ts → descending id
        assert [it.id for it in page1] == sorted([a.id for a in arts], reverse=True)[:2]

    async def test_oldest_sort_cursor(self, pg):
        user, feed = await _setup(pg)
        arts = [await _art(pg, feed, published_at=NOW - timedelta(hours=i)) for i in range(4)]
        # oldest first → a[3] (oldest) .. a[0] (newest)
        page1 = await list_articles(user, pg, feed_id=feed.id, sort_order="oldest", limit=2)
        assert [it.id for it in page1] == [arts[3].id, arts[2].id]
        page2 = await list_articles(
            user, pg, feed_id=feed.id, sort_order="oldest", limit=2, **_cursor(page1[-1])
        )
        assert [it.id for it in page2] == [arts[1].id, arts[0].id]

    async def test_cursor_supersedes_offset(self, pg):
        """When a cursor is given, offset is ignored (web sends one or the other)."""
        user, feed = await _setup(pg)
        arts = [await _art(pg, feed, published_at=NOW - timedelta(hours=i)) for i in range(4)]
        page1 = await list_articles(user, pg, feed_id=feed.id, limit=2)
        # huge offset would have returned nothing under offset pagination
        page2 = await list_articles(
            user, pg, feed_id=feed.id, limit=2, offset=999, **_cursor(page1[-1])
        )
        assert [it.id for it in page2] == [arts[2].id, arts[3].id]


class TestMoreQsBuilder:
    def test_search_uses_offset(self):
        qs = _build_more_qs({"q": "foo"}, [ArticleListItem(
            id=1, feed_id=1, feed_title="f", url="u", title="t", author=None,
            summary=None, snippet=None, published_at=NOW, formatted_date="x",
            estimated_read_min=None, image_url=None, is_read=False, is_starred=False,
            is_archived=False, sort_ts=NOW,
        )], q="foo", next_offset=50)
        assert "offset=50" in qs
        assert "cursor_ts" not in qs

    def test_non_search_uses_cursor(self):
        item = ArticleListItem(
            id=7, feed_id=1, feed_title="f", url="u", title="t", author=None,
            summary=None, snippet=None, published_at=NOW, formatted_date="x",
            estimated_read_min=None, image_url=None, is_read=False, is_starred=False,
            is_archived=False, sort_ts=NOW,
        )
        qs = _build_more_qs({"unread_only": "true"}, [item], q=None, next_offset=50)
        assert "cursor_id=7" in qs
        assert "cursor_ts" in qs
        assert "offset" not in qs


def test_sort_ts_excluded_from_serialization():
    item = ArticleListItem(
        id=1, feed_id=1, feed_title="f", url="u", title="t", author=None,
        summary=None, snippet=None, published_at=NOW, formatted_date="x",
        estimated_read_min=None, image_url=None, is_read=False, is_starred=False,
        is_archived=False, sort_ts=NOW,
    )
    assert "sort_ts" not in item.model_dump()
    assert item.sort_ts == NOW  # still accessible on the object

"""Full-text search must reach everything the user may access — and nothing else.

Search has no state anchor of its own (no `is_starred == True` to scope it), so it
carries article_access_predicate explicitly. That makes it the one listing branch
where a mistake leaks other users' articles, hence the isolation tests here.

Runs against the real (dev) database inside a transaction that is always rolled
back. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.article import list_articles

NOW = datetime.now(timezone.utc)
TERM = "zzsearchable"


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


async def _user(session) -> User:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"srch_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    return user


async def _feed(session, user=None) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"feed-{u[:6]}",
                subscriber_count=1 if user else 0)
    session.add(feed)
    await session.flush()
    if user is not None:
        session.add(UserFeed(user_id=user.id, feed_id=feed.id))
        await session.flush()
    return feed


async def _article(session, feed=None, *, title=f"A {TERM} article") -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id if feed else None, guid=u, guid_hash=u, title=title,
        content="<p>body</p>", readable_status="success",
        published_at=NOW, fetched_at=NOW,
    )
    session.add(a)
    await session.flush()
    return a


async def _state(session, user, article, **kw) -> UserArticleState:
    st = UserArticleState(user_id=user.id, article_id=article.id, **kw)
    session.add(st)
    await session.flush()
    return st


async def _search(session, user):
    return await list_articles(user=user, db=session, q=TERM, limit=50)


class TestSearchReachesAccessibleArticles:
    async def test_saved_article_is_findable(self, pg):
        """The reported gap: a saved-by-URL article has no feed, so the subscription
        join dropped it and search could never find what you deliberately kept."""
        user = await _user(pg)
        art = await _article(pg, None)
        await _state(pg, user, art, saved_at=NOW)

        found = await _search(pg, user)
        assert art.id in [a.id for a in found]

    async def test_subscribed_feed_article_is_findable(self, pg):
        user = await _user(pg)
        feed = await _feed(pg, user)
        art = await _article(pg, feed)

        found = await _search(pg, user)
        assert art.id in [a.id for a in found]

    async def test_starred_orphan_is_findable(self, pg):
        """Same class of gap, previously invisible: unsubscribing nulls feed_id on
        starred articles, which used to make them unsearchable."""
        user = await _user(pg)
        art = await _article(pg, None)
        await _state(pg, user, art, is_starred=True)

        found = await _search(pg, user)
        assert art.id in [a.id for a in found]


class TestSearchIsolation:
    async def test_another_users_saved_article_is_not_findable(self, pg):
        owner, other = await _user(pg), await _user(pg)
        art = await _article(pg, None)
        await _state(pg, owner, art, saved_at=NOW)

        assert art.id not in [a.id for a in await _search(pg, other)]

    async def test_unsubscribed_feed_article_is_not_findable(self, pg):
        """A feed nobody in this test subscribes to: search must not surface it just
        because the joins became optional."""
        other = await _user(pg)
        feed = await _feed(pg, None)
        art = await _article(pg, feed)

        assert art.id not in [a.id for a in await _search(pg, other)]

    async def test_article_with_a_bare_state_row_is_not_findable(self, pg):
        """A state row alone is not access — read/dwell tracking can leave one behind
        after an unsubscribe without starring, archiving or saving."""
        user = await _user(pg)
        art = await _article(pg, None)
        await _state(pg, user, art, is_read=True)

        assert art.id not in [a.id for a in await _search(pg, user)]

    async def test_unsaving_removes_it_from_search_again(self, pg):
        user = await _user(pg)
        art = await _article(pg, None)
        st = await _state(pg, user, art, saved_at=NOW)
        assert art.id in [a.id for a in await _search(pg, user)]

        st.saved_at = None
        await pg.flush()
        assert art.id not in [a.id for a in await _search(pg, user)]

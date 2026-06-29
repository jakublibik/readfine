"""Multi-select scope filtering for the article listing (search "Search in").

`list_articles(scope_include=...)` accepts the same JSON scope format as filters
and catchup (["feed:1", "folder:2"]) and restricts the listing to the selected
feeds/folders. Empty/None means "all feeds". folder:0 is the no-folder sentinel.

Runs against the real (dev) database inside a transaction that is always rolled
back; scoped to throwaway feeds. Skips automatically if the DB is unreachable.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed, Folder, UserFeed
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


async def _feed(session, user, *, folder_id=None) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"feed-{u[:6]}", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id, folder_id=folder_id))
    await session.flush()
    return feed


async def _art(session, feed) -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title="T",
        content="<p>body</p>", readable_status="pending",
        published_at=NOW, fetched_at=NOW,
    )
    session.add(a)
    await session.flush()
    return a


async def _setup(session):
    """One user, two folders + a no-folder feed; one article per feed."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"scope_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()

    fa = Folder(user_id=user.id, name=f"A-{u}")
    fb = Folder(user_id=user.id, name=f"B-{u}")
    session.add_all([fa, fb])
    await session.flush()

    feed_a = await _feed(session, user, folder_id=fa.id)
    feed_b = await _feed(session, user, folder_id=fb.id)
    feed_n = await _feed(session, user, folder_id=None)

    art_a = await _art(session, feed_a)
    art_b = await _art(session, feed_b)
    art_n = await _art(session, feed_n)
    return user, (fa, fb), (feed_a, feed_b, feed_n), (art_a, art_b, art_n)


async def _ids(session, user, scope_include):
    items = await list_articles(user=user, db=session, scope_include=scope_include, limit=100)
    return {i.id for i in items}


async def test_scope_none_returns_all(pg):
    user, _, _, (a, b, n) = await _setup(pg)
    assert await _ids(pg, user, None) == {a.id, b.id, n.id}


async def test_scope_empty_string_returns_all(pg):
    user, _, _, (a, b, n) = await _setup(pg)
    assert await _ids(pg, user, "") == {a.id, b.id, n.id}
    assert await _ids(pg, user, "[]") == {a.id, b.id, n.id}


async def test_scope_single_feed(pg):
    user, _, (feed_a, _, _), (a, b, n) = await _setup(pg)
    assert await _ids(pg, user, json.dumps([f"feed:{feed_a.id}"])) == {a.id}


async def test_scope_single_folder(pg):
    user, (fa, _), _, (a, b, n) = await _setup(pg)
    assert await _ids(pg, user, json.dumps([f"folder:{fa.id}"])) == {a.id}


async def test_scope_no_folder_sentinel(pg):
    user, _, _, (a, b, n) = await _setup(pg)
    assert await _ids(pg, user, json.dumps(["folder:0"])) == {n.id}


async def test_scope_multi_select_union(pg):
    user, (fa, _), (_, feed_b, _), (a, b, n) = await _setup(pg)
    scope = json.dumps([f"folder:{fa.id}", f"feed:{feed_b.id}"])
    assert await _ids(pg, user, scope) == {a.id, b.id}


async def test_scope_foreign_feed_id_matches_nothing(pg):
    """Feed ownership is enforced by the UserFeed join — an unknown id yields no rows."""
    user, _, _, _ = await _setup(pg)
    assert await _ids(pg, user, json.dumps(["feed:999999999"])) == set()

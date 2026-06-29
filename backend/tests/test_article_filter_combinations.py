"""Combined search/filter criteria for the article listing.

The individual knobs are covered in test_article_scope / _search / _label_filter;
this file locks their *intersection* (AND semantics) — scope × read status ×
labels, with and without a text query — since that is the realistic way the
search modal submits.

Unique nonsense token in the titles keeps the FTS match scoped to these rows.
Runs against the real (dev) DB in a rolled-back transaction; skips if unreachable.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, Folder, UserFeed
from app.models.label import ArticleLabel, Label
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


async def _feed(session, user, folder_id):
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"f-{u[:6]}", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id, folder_id=folder_id))
    await session.flush()
    return feed


async def _art(session, feed, token, *, read=False, label_id=None, user=None):
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=f"{token} {u[:6]}",
        content="<p>body</p>", readable_status="pending",
        published_at=NOW, fetched_at=NOW,
    )
    session.add(a)
    await session.flush()
    if read:
        session.add(UserArticleState(user_id=user.id, article_id=a.id, is_read=True))
    if label_id is not None:
        session.add(ArticleLabel(user_id=user.id, article_id=a.id, label_id=label_id))
    await session.flush()
    return a


async def _setup(session):
    u = uuid.uuid4().hex[:12]
    user = User(email=f"combo_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()

    fa = Folder(user_id=user.id, name=f"A-{u}")
    fb = Folder(user_id=user.id, name=f"B-{u}")
    session.add_all([fa, fb])
    await session.flush()

    feed_a = await _feed(session, user, fa.id)
    feed_b = await _feed(session, user, fb.id)

    l1 = Label(user_id=user.id, name=f"L1-{u}", color="#111111")
    l2 = Label(user_id=user.id, name=f"L2-{u}", color="#222222")
    session.add_all([l1, l2])
    await session.flush()

    token = "zcombotok" + uuid.uuid4().hex[:10]
    # folder A: one unread+L1, one read (no label)
    a_unread_l1 = await _art(session, feed_a, token, label_id=l1.id, user=user)
    a_read = await _art(session, feed_a, token, read=True, user=user)
    # folder B: one unread+L2
    b_unread_l2 = await _art(session, feed_b, token, label_id=l2.id, user=user)

    return {
        "user": user, "token": token,
        "fa": fa, "fb": fb, "l1": l1, "l2": l2,
        "a_unread_l1": a_unread_l1, "a_read": a_read, "b_unread_l2": b_unread_l2,
    }


async def _ids(session, ctx, **kw):
    items = await list_articles(user=ctx["user"], db=session, limit=100, **kw)
    return {i.id for i in items}


async def test_query_scope_and_status(pg):
    """q + folder scope + unread → only the unread article in that folder."""
    ctx = await _setup(pg)
    ids = await _ids(
        pg, ctx, q=ctx["token"],
        scope_include=json.dumps([f"folder:{ctx['fa'].id}"]),
        read_status="unread",
    )
    assert ids == {ctx["a_unread_l1"].id}


async def test_query_scope_and_status_read(pg):
    """q + folder scope + read → only the read article in that folder."""
    ctx = await _setup(pg)
    ids = await _ids(
        pg, ctx, q=ctx["token"],
        scope_include=json.dumps([f"folder:{ctx['fa'].id}"]),
        read_status="read",
    )
    assert ids == {ctx["a_read"].id}


async def test_query_scope_and_specific_label(pg):
    """q + folder scope + specific label → intersection of all three."""
    ctx = await _setup(pg)
    ids = await _ids(
        pg, ctx, q=ctx["token"],
        scope_include=json.dumps([f"folder:{ctx['fa'].id}"]),
        label_filter=json.dumps([f"label:{ctx['l1'].id}"]),
    )
    assert ids == {ctx["a_unread_l1"].id}


async def test_label_in_other_folder_excluded_by_scope(pg):
    """Scope and label AND: L2 lives in folder B, so scoping to A yields nothing."""
    ctx = await _setup(pg)
    ids = await _ids(
        pg, ctx, q=ctx["token"],
        scope_include=json.dumps([f"folder:{ctx['fa'].id}"]),
        label_filter=json.dumps([f"label:{ctx['l2'].id}"]),
    )
    assert ids == set()


async def test_filter_view_no_query_scope_and_status(pg):
    """Empty query (filter view): scope B + unread → the unread B article only."""
    ctx = await _setup(pg)
    ids = await _ids(
        pg, ctx, q=None,
        scope_include=json.dumps([f"folder:{ctx['fb'].id}"]),
        read_status="unread",
    )
    assert ids == {ctx["b_unread_l2"].id}


async def test_query_any_label_spans_folders(pg):
    """q + any-label spans both folders, excludes the unlabelled read article."""
    ctx = await _setup(pg)
    ids = await _ids(pg, ctx, q=ctx["token"], label_filter=json.dumps(["any"]))
    assert ids == {ctx["a_unread_l1"].id, ctx["b_unread_l2"].id}

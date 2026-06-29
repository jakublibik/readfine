"""Search label filter for the article listing.

`list_articles(label_filter=...)` accepts a JSON array (same shape as the scope
selector): ["any"] matches articles with at least one label; ["label:3", ...]
matches articles carrying at least one of those labels (OR). Empty/None = no
label filtering.

Unique nonsense token in titles keeps the full-text match scoped to these rows.
Runs against the real (dev) DB in a rolled-back transaction; skips if unreachable.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed, UserFeed
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


async def _art(session, feed, *, title) -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=title,
        content="<p>body</p>", readable_status="pending",
        published_at=NOW, fetched_at=NOW,
    )
    session.add(a)
    await session.flush()
    return a


async def _setup(session):
    """User/feed; three matching articles: one with L1, one with L2, one unlabeled."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"lbl_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()

    l1 = Label(user_id=user.id, name=f"L1-{u}", color="#111111")
    l2 = Label(user_id=user.id, name=f"L2-{u}", color="#222222")
    session.add_all([l1, l2])
    await session.flush()

    token = "zlbltok" + uuid.uuid4().hex[:10]
    a1 = await _art(session, feed, title=f"{token} one")
    a2 = await _art(session, feed, title=f"{token} two")
    a3 = await _art(session, feed, title=f"{token} three")
    session.add(ArticleLabel(user_id=user.id, article_id=a1.id, label_id=l1.id))
    session.add(ArticleLabel(user_id=user.id, article_id=a2.id, label_id=l2.id))
    await session.flush()
    return user, token, (l1, l2), (a1, a2, a3)


async def _ids(session, user, token, label_filter):
    items = await list_articles(user=user, db=session, q=token, label_filter=label_filter, limit=100)
    return {i.id for i in items}


async def test_no_filter_returns_all(pg):
    user, token, _, (a1, a2, a3) = await _setup(pg)
    assert await _ids(pg, user, token, None) == {a1.id, a2.id, a3.id}


async def test_any_label_excludes_unlabeled(pg):
    user, token, _, (a1, a2, a3) = await _setup(pg)
    assert await _ids(pg, user, token, json.dumps(["any"])) == {a1.id, a2.id}


async def test_single_label(pg):
    user, token, (l1, _), (a1, a2, a3) = await _setup(pg)
    assert await _ids(pg, user, token, json.dumps([f"label:{l1.id}"])) == {a1.id}


async def test_multiple_labels_union(pg):
    user, token, (l1, l2), (a1, a2, a3) = await _setup(pg)
    scope = json.dumps([f"label:{l1.id}", f"label:{l2.id}"])
    assert await _ids(pg, user, token, scope) == {a1.id, a2.id}


async def test_any_takes_precedence_over_ids(pg):
    user, token, (l1, _), (a1, a2, a3) = await _setup(pg)
    # "any" present alongside a specific id → behaves as "any".
    scope = json.dumps(["any", f"label:{l1.id}"])
    assert await _ids(pg, user, token, scope) == {a1.id, a2.id}


async def test_foreign_label_id_matches_nothing(pg):
    user, token, _, _ = await _setup(pg)
    assert await _ids(pg, user, token, json.dumps(["label:999999999"])) == set()


async def test_label_filter_without_query(pg):
    """Filter-view path: label filter applies even with no search term."""
    user, _, (l1, _), (a1, a2, a3) = await _setup(pg)
    items = await list_articles(
        user=user, db=pg, q=None, label_filter=json.dumps([f"label:{l1.id}"]), limit=100
    )
    # No FTS term, so other dev-DB articles may appear; assert our labelled one is
    # present and the unlabelled siblings are not.
    ids = {i.id for i in items}
    assert a1.id in ids
    assert a2.id not in ids and a3.id not in ids

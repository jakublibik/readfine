"""Sidebar badges against the lists they stand above.

The sidebar counts every feed, folder and label in grouped queries of its own, while
a single view is counted through the list's query (count_articles). The two must give
the same number for every view, or a badge promises rows the list does not have.
Runs against the real database inside a transaction that is always rolled back.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, Folder, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.services.counts_service import mark_read_total, sidebar_counts, view_badge
from app.services.story_service import DEDUP_COLLAPSE, DEDUP_OFF

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


async def _add(session, obj):
    session.add(obj)
    await session.flush()
    return obj


async def _feed(session, user, folder=None) -> Feed:
    u = uuid.uuid4().hex[:12]
    feed = await _add(session, Feed(feed_url=f"https://ex.invalid/{u}.xml", title=u,
                                    subscriber_count=1))
    await _add(session, UserFeed(user_id=user.id, feed_id=feed.id,
                                 folder_id=folder.id if folder else None))
    return feed


async def _article(session, feed, *, story_id=None, trimmed=False) -> Article:
    u = uuid.uuid4().hex
    return await _add(session, Article(
        feed_id=feed.id, guid=u, guid_hash=u, title="T", story_id=story_id,
        published_at=NOW - timedelta(minutes=5), fetched_at=NOW - timedelta(minutes=5),
        trimmed_at=NOW if trimmed else None,
    ))


@pytest_asyncio.fixture
async def reader(pg):
    """One reader with something in every view, including the cases that drifted:

    - a story across two feeds of one folder (one row in the folder, two in the feeds),
    - a retention stub, starred after it was trimmed (hidden from every list),
    - an article with two labels (one row under Labels, even with nothing folding),
    - a read article and a starred one.
    """
    u = uuid.uuid4().hex[:12]
    user = await _add(pg, User(email=f"counts_{u}@test.invalid", password_hash="x",
                               display_name="t"))
    folder = await _add(pg, Folder(user_id=user.id, name="News"))
    a = await _feed(pg, user, folder)
    b = await _feed(pg, user, folder)
    loose = await _feed(pg, user)

    head = await _article(pg, a)
    head.story_id = head.id
    await pg.flush()
    await _article(pg, b, story_id=head.id)
    read = await _article(pg, a)
    starred = await _article(pg, loose)
    stub = await _article(pg, a, trimmed=True)
    double = await _article(pg, loose)

    pg.add_all([
        UserArticleState(user_id=user.id, article_id=read.id, is_read=True),
        UserArticleState(user_id=user.id, article_id=starred.id, is_starred=True),
        UserArticleState(user_id=user.id, article_id=stub.id, is_starred=True),
    ])
    red = await _add(pg, Label(user_id=user.id, name="red"))
    blue = await _add(pg, Label(user_id=user.id, name="blue"))
    pg.add_all([
        ArticleLabel(user_id=user.id, article_id=double.id, label_id=red.id),
        ArticleLabel(user_id=user.id, article_id=double.id, label_id=blue.id),
        ArticleLabel(user_id=user.id, article_id=head.id, label_id=red.id),
        ArticleLabel(user_id=user.id, article_id=stub.id, label_id=red.id),
    ])
    await pg.flush()
    return user, folder, [a, b, loose], [red, blue]


@pytest.mark.parametrize("dedup", [DEDUP_COLLAPSE, DEDUP_OFF])
async def test_every_sidebar_badge_matches_its_list(pg, reader, dedup):
    user, folder, feeds, labels = reader
    c = await sidebar_counts(
        pg, user.id, feed_ids=[f.id for f in feeds], label_ids=[lb.id for lb in labels],
        story_dedup=dedup,
    )

    async def badge(**view):
        return await view_badge(user, pg, story_dedup=dedup, **view)

    assert (c.nav_unread, c.nav_total) == await badge()
    assert (c.folder_unread_counts[folder.id], c.folder_total_counts[folder.id]) \
        == await badge(folder_id=folder.id)
    assert (c.folder_unread_counts[None], c.folder_total_counts[None]) \
        == await badge(folder_id=0)
    for f in feeds:
        assert (c.feed_unread_counts.get(f.id, 0), c.feed_total_counts.get(f.id, 0)) \
            == await badge(feed_id=f.id)
    assert (c.nav_unread_starred, c.nav_starred) == await badge(starred_only=True)
    assert (c.nav_unread_labeled, c.nav_labeled) == await badge(labeled_only=True)
    for lb in labels:
        assert (c.label_unread_counts.get(lb.id, 0), c.label_counts.get(lb.id, 0)) \
            == await badge(label_id=lb.id)


async def test_the_numbers_themselves(pg, reader):
    """Pinned once, so the test above can't pass by both sides agreeing on a wrong
    number."""
    user, folder, feeds, labels = reader
    a, b, loose = feeds
    red, _ = labels
    c = await sidebar_counts(pg, user.id, feed_ids=[a.id, b.id, loose.id],
                             label_ids=[red.id], story_dedup=DEDUP_COLLAPSE)
    # Story (one row), read, starred, double. The stub is gone.
    assert c.nav_total == 4
    assert c.folder_total_counts[folder.id] == 2
    assert (c.feed_total_counts[a.id], c.feed_total_counts[b.id]) == (2, 1)
    assert c.nav_starred == 1
    assert c.nav_labeled == 2
    assert c.label_counts[red.id] == 2

    off = await sidebar_counts(pg, user.id, feed_ids=[], label_ids=[],
                               story_dedup=DEDUP_OFF)
    assert off.nav_total == 5
    assert off.folder_total_counts[folder.id] == 3
    # Two labels on one article are still one row.
    assert off.nav_labeled == 2


async def test_mark_read_total_counts_the_view_it_marked(pg, reader):
    user, _, _, labels = reader
    red, _ = labels
    assert await mark_read_total(user, pg) == 4
    assert await mark_read_total(user, pg, starred_only=True) == 1
    assert await mark_read_total(user, pg, label_id=red.id) == 2
    # Several flags: the first one in mark_scope_read's order wins.
    assert await mark_read_total(user, pg, starred_only=True, labeled_only=True) == 1

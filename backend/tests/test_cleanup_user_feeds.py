"""Account deletion: what ``cleanup_user_feeds`` leaves behind.

An article another reader keeps for good (starred, archived, saved) outlives the
account; one kept only by the account being deleted goes with it, whether it came
through a feed or was saved by URL.

Runs against the real (dev) DB in a rolled-back transaction and skips if unreachable.
"""
import uuid
from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.feed import cleanup_user_feeds


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


async def _user(pg, tag):
    user = User(email=f"{tag}_{uuid.uuid4().hex[:12]}@test.invalid",
                password_hash="x", display_name="t")
    pg.add(user)
    await pg.flush()
    return user


async def _feed(pg, *subscribers):
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="f",
                subscriber_count=len(subscribers))
    pg.add(feed)
    await pg.flush()
    for s in subscribers:
        pg.add(UserFeed(user_id=s.id, feed_id=feed.id))
    await pg.flush()
    return feed


async def _article(pg, feed=None):
    u = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    a = Article(
        feed_id=feed.id if feed else None, guid=u, guid_hash=u, title="T",
        url=f"https://ex.invalid/{u}", url_normalized=f"https://ex.invalid/{u}",
        content="<p>b</p>", readable_status="success", published_at=now, fetched_at=now,
    )
    pg.add(a)
    await pg.flush()
    return a


async def _state(pg, user, article, **kw):
    pg.add(UserArticleState(user_id=user.id, article_id=article.id, **kw))
    await pg.flush()


async def _delete_account(pg, user):
    await cleanup_user_feeds(user.id, pg)
    await pg.delete(user)
    await pg.flush()
    pg.expunge_all()


async def _article_row(pg, article_id):
    return await pg.scalar(select(Article).where(Article.id == article_id))


class TestCleanupUserFeeds:
    async def test_own_star_in_last_feed_does_not_keep_the_article(self, pg):
        user = await _user(pg, "own")
        feed = await _feed(pg, user)
        a = await _article(pg, feed)
        await _state(pg, user, a, is_starred=True)
        aid, fid = a.id, feed.id

        await _delete_account(pg, user)

        assert await _article_row(pg, aid) is None
        assert await pg.scalar(select(Feed).where(Feed.id == fid)) is None

    async def test_article_another_reader_starred_survives_detached(self, pg):
        user = await _user(pg, "gone")
        other = await _user(pg, "stays")
        feed = await _feed(pg, user)
        a = await _article(pg, feed)
        await _state(pg, user, a, is_starred=True)
        await _state(pg, other, a, is_starred=True)
        aid = a.id

        await _delete_account(pg, user)

        survivor = await _article_row(pg, aid)
        assert survivor is not None and survivor.feed_id is None

    async def test_own_saved_by_url_article_goes(self, pg):
        user = await _user(pg, "saver")
        a = await _article(pg)
        await _state(pg, user, a, saved_at=datetime.now(timezone.utc))
        aid = a.id

        await _delete_account(pg, user)

        assert await _article_row(pg, aid) is None

    async def test_saved_by_url_article_another_reader_saved_survives(self, pg):
        user = await _user(pg, "saver")
        other = await _user(pg, "cosaver")
        a = await _article(pg)
        now = datetime.now(timezone.utc)
        await _state(pg, user, a, saved_at=now)
        await _state(pg, other, a, saved_at=now)
        aid = a.id

        await _delete_account(pg, user)

        assert await _article_row(pg, aid) is not None

    async def test_shared_feed_and_its_articles_are_untouched(self, pg):
        user = await _user(pg, "leaver")
        other = await _user(pg, "reader")
        feed = await _feed(pg, user, other)
        a = await _article(pg, feed)
        await _state(pg, user, a, is_starred=True)
        aid, fid = a.id, feed.id

        await _delete_account(pg, user)

        row = await _article_row(pg, aid)
        assert row is not None and row.feed_id == fid
        feed_row = await pg.scalar(select(Feed).where(Feed.id == fid))
        assert feed_row.subscriber_count == 1

    async def test_feedless_article_without_own_state_is_untouched(self, pg):
        """Only articles this account held a state on are its to clean up."""
        user = await _user(pg, "leaver")
        other = await _user(pg, "owner")
        a = await _article(pg)
        await _state(pg, other, a, saved_at=datetime.now(timezone.utc))
        aid = a.id

        await _delete_account(pg, user)

        assert await _article_row(pg, aid) is not None

"""An article a reader opens while it waits in the readable queue is extracted then.

The batch worker takes the whole instance in id order, 20 a minute, so after a large
import an opened article could wait hours for its full text. claim_queued_readable
takes it out of the queue for the extraction the open starts.

Runs against the real (dev) DB in a rolled-back transaction and skips if unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed
from app.services.readable_service import claim_queued_readable


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
    session = AsyncSession(bind=conn, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


@pytest_asyncio.fixture
async def feed(pg):
    u = uuid.uuid4().hex
    f = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"feed-{u[:6]}", subscriber_count=1)
    pg.add(f)
    await pg.flush()
    return f


async def _article(pg, feed_id, **fields):
    g = uuid.uuid4().hex
    a = Article(feed_id=feed_id, guid=g, guid_hash=g, title="x", content="<p>x</p>",
                url=f"https://ex.invalid/{g}", **{"readable_status": "pending", **fields})
    pg.add(a)
    await pg.flush()
    return a


async def _next_retry(pg, article_id):
    return await pg.scalar(
        select(Article.readable_next_retry_at).where(Article.id == article_id))


async def test_queued_article_is_claimed(pg, feed):
    a = await _article(pg, feed.id)
    assert await claim_queued_readable(pg, a.id)
    # Pushed out of the batch's reach, but not for long: a lost attempt comes back.
    until = await _next_retry(pg, a.id)
    now = datetime.now(timezone.utc)
    assert now < until <= now + timedelta(minutes=10)


async def test_second_open_does_not_claim_again(pg, feed):
    a = await _article(pg, feed.id)
    assert await claim_queued_readable(pg, a.id)
    assert not await claim_queued_readable(pg, a.id)


async def test_article_in_backoff_keeps_its_schedule(pg, feed):
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    a = await _article(pg, feed.id, readable_retries=1, readable_next_retry_at=later)
    assert not await claim_queued_readable(pg, a.id)
    assert await _next_retry(pg, a.id) == later


async def test_only_pending_feed_articles(pg, feed):
    done = await _article(pg, feed.id, readable_status="success")
    saved = await _article(pg, None)  # saved by URL: its own extraction path
    trimmed = await _article(pg, feed.id, trimmed_at=datetime.now(timezone.utc))
    for a in (done, saved, trimmed):
        assert not await claim_queued_readable(pg, a.id)

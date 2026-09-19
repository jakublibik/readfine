"""Tests for the story backfill's destructive half (app.scripts.backfill_stories).

Only ``_reset`` is covered here, and it is covered because it is the one operation in
this feature that takes data away: it clears every group in the database and undoes
every read this feature has written. Getting its WHERE clause wrong would either leave
articles hidden under rules that no longer exist, or mark a pile of genuinely read
articles unread.

The scan and the assignment are exercised through ``test_story_dedup.py``, which drives
the same membership rule through the live path.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed
from app.models.user import User
from app.scripts.backfill_stories import _reset

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


async def _fixtures(pg, suppressed_by: str | None):
    """A user, an article, and one state row in whatever condition the test needs."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"bf_{u}@test.invalid", password_hash="x", display_name="t")
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    pg.add_all([user, feed])
    await pg.flush()

    article = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=f"{u} a headline long enough",
        published_at=NOW - timedelta(hours=1), fetched_at=NOW - timedelta(hours=1),
        story_id=None,
    )
    pg.add(article)
    await pg.flush()
    article.story_id = article.id

    state = UserArticleState(
        user_id=user.id, article_id=article.id,
        is_read=suppressed_by is not None,
        read_at=NOW if suppressed_by else None,
        suppressed_at=NOW if suppressed_by else None,
        suppressed_by=suppressed_by,
        hidden_at=NOW if suppressed_by else None,
    )
    pg.add(state)
    await pg.flush()
    return article, state


async def _reload(pg, state):
    """Read the row back from the database, not from the session's identity map.

    _reset writes in SQL, so the object the test is holding is stale and would answer
    with what the test itself put there — which is to say every assertion here would
    pass whatever _reset did.
    """
    return (await pg.execute(
        select(UserArticleState)
        .where(
            UserArticleState.user_id == state.user_id,
            UserArticleState.article_id == state.article_id,
        )
        .execution_options(populate_existing=True)
    )).scalar_one()


class TestReset:
    async def test_groups_are_cleared(self, pg):
        article, _ = await _fixtures(pg, None)
        await _reset(pg)
        assert (await pg.execute(
            select(Article.story_id).where(Article.id == article.id)
        )).scalar_one() is None

    async def test_a_hidden_article_comes_back_unread(self, pg):
        """The whole point: nobody read it, a rule that no longer exists hid it."""
        _, state = await _fixtures(pg, "similar")
        await _reset(pg)
        fresh = await _reload(pg, state)
        assert fresh.is_read is False
        assert fresh.read_at is None
        assert fresh.suppressed_at is None
        assert fresh.suppressed_by is None

    async def test_an_article_folded_under_a_read_story_comes_back_too(self, pg):
        _, state = await _fixtures(pg, "story")
        await _reset(pg)
        assert (await _reload(pg, state)).is_read is False

    async def test_url_dedup_is_left_alone(self, pg):
        """A different feature, keyed on an identical address, and it is not wrong."""
        _, state = await _fixtures(pg, "url")
        await _reset(pg)
        fresh = await _reload(pg, state)
        assert fresh.is_read is True
        assert fresh.suppressed_by == "url"

    async def test_a_genuine_read_is_left_alone(self, pg):
        """No suppressed_at means a person read it, and that is not ours to undo."""
        _, state = await _fixtures(pg, None)
        state.is_read = True
        state.read_at = NOW
        await pg.flush()

        await _reset(pg)

        assert (await _reload(pg, state)).is_read is True

    async def test_the_record_that_something_was_hidden_survives(self, pg):
        """hidden_at is a record, not a decision: nothing re-reads it, nothing clears it."""
        _, state = await _fixtures(pg, "similar")
        await _reset(pg)
        assert (await _reload(pg, state)).hidden_at is not None

    async def test_it_reports_what_it_gave_back(self, pg):
        _, state = await _fixtures(pg, "similar")
        assert await _reset(pg) >= 1

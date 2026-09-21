"""Missed gems against story groups.

A gem is a high score the reader never spent any time on. Scoring is per article,
a story is one piece of news carried by several articles, so the two have to be
reconciled: reading any member means the news was not missed, and a group nobody
read is still only one thing to catch up on, not five.

Runs against the real database inside a transaction that is always rolled back.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.stats_service import get_ai_stats

NOW = datetime.now(timezone.utc)
RECENT = NOW - timedelta(hours=1)


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


async def _user(pg) -> User:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"gems_{u}@test.invalid", password_hash="x", display_name="t")
    pg.add(user)
    await pg.flush()
    return user


async def _feed(pg, user) -> Feed:
    u = uuid.uuid4().hex[:12]
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"Feed {u[:4]}",
                subscriber_count=1)
    pg.add(feed)
    await pg.flush()
    pg.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await pg.flush()
    return feed


async def _article(pg, feed, title="T") -> Article:
    u = uuid.uuid4().hex
    article = Article(feed_id=feed.id, guid=u, guid_hash=u, title=title,
                      published_at=RECENT, fetched_at=RECENT)
    pg.add(article)
    await pg.flush()
    return article


async def _state(pg, user, article, *, score, dwell=0, link_opened=False):
    pg.add(UserArticleState(user_id=user.id, article_id=article.id, ai_score=score,
                            dwell_seconds=dwell, link_opened=link_opened))
    await pg.flush()


async def _group(pg, *articles) -> None:
    """Put the articles in one story, named after the first of them."""
    for article in articles:
        article.story_id = articles[0].id
    await pg.flush()


async def _gem_ids(pg, user) -> list[int]:
    stats = await get_ai_stats(user.id, pg)
    return [g.article_id for g in stats.gems]


class TestGemsOutsideAnyStory:
    async def test_a_high_score_nobody_touched_is_a_gem(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _state(pg, user, article, score=0.9)

        assert await _gem_ids(pg, user) == [article.id]

    async def test_unrelated_articles_are_not_folded_together(self, pg):
        """Two articles with no story_id each key on their own id, so the one-per-group
        rule must not treat them as the same group."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        first = await _article(pg, feed)
        second = await _article(pg, feed)
        await _state(pg, user, first, score=0.9)
        await _state(pg, user, second, score=0.8)

        assert await _gem_ids(pg, user) == [first.id, second.id]


class TestReadingTheStoryClosesIt:
    async def test_time_spent_on_a_sibling_drops_the_group(self, pg):
        user = await _user(pg)
        feed = await _feed(pg, user)
        read, missed = await _article(pg, feed), await _article(pg, feed)
        await _group(pg, read, missed)
        await _state(pg, user, read, score=0.9, dwell=45)
        await _state(pg, user, missed, score=0.85)

        assert await _gem_ids(pg, user) == []

    async def test_opening_a_sibling_link_drops_the_group(self, pg):
        user = await _user(pg)
        feed = await _feed(pg, user)
        read, missed = await _article(pg, feed), await _article(pg, feed)
        await _group(pg, read, missed)
        await _state(pg, user, read, score=0.9, link_opened=True)
        await _state(pg, user, missed, score=0.85)

        assert await _gem_ids(pg, user) == []

    async def test_a_glance_at_a_sibling_is_not_reading_it(self, pg):
        """Under half a minute is the same as no time at all everywhere else, and the
        group stays missed."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        glanced, missed = await _article(pg, feed), await _article(pg, feed)
        await _group(pg, glanced, missed)
        await _state(pg, user, glanced, score=0.75, dwell=5)
        await _state(pg, user, missed, score=0.85)

        assert await _gem_ids(pg, user) == [missed.id]

    async def test_another_readers_time_does_not_count(self, pg):
        user = await _user(pg)
        other = await _user(pg)
        feed = await _feed(pg, user)
        pg.add(UserFeed(user_id=other.id, feed_id=feed.id))
        await pg.flush()
        read, missed = await _article(pg, feed), await _article(pg, feed)
        await _group(pg, read, missed)
        await _state(pg, other, read, score=0.9, dwell=120)
        await _state(pg, user, missed, score=0.85)

        assert await _gem_ids(pg, user) == [missed.id]


class TestOneRowPerStory:
    async def test_the_group_is_listed_once_at_its_best_score(self, pg):
        user = await _user(pg)
        feed = await _feed(pg, user)
        weaker, best, third = (await _article(pg, feed), await _article(pg, feed),
                               await _article(pg, feed))
        await _group(pg, weaker, best, third)
        await _state(pg, user, weaker, score=0.75)
        await _state(pg, user, best, score=0.95)
        await _state(pg, user, third, score=0.8)

        assert await _gem_ids(pg, user) == [best.id]

    async def test_a_folded_group_does_not_cost_another_story_its_place(self, pg):
        """The limit is ten rows. Before the fold, one event with three high-scoring
        sources spent three of them."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        members = [await _article(pg, feed) for _ in range(3)]
        await _group(pg, *members)
        for member in members:
            await _state(pg, user, member, score=0.9)
        alone = await _article(pg, feed)
        await _state(pg, user, alone, score=0.71)

        assert await _gem_ids(pg, user) == [members[0].id, alone.id]

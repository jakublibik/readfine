"""Integration tests for the reading side of story grouping (story_service).

The one thing that must not go wrong here is access: story_id is global, so a group
routinely holds articles from feeds the reader never subscribed to, and the footer is
the first place that could hand them over. Runs against the real database inside a
transaction that is always rolled back.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.story_service import MEMBER_LIMIT, count_members, list_members

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


async def _user(session) -> User:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"member_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    return user


async def _feed(session, *subscribers) -> Feed:
    u = uuid.uuid4().hex[:12]
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"Feed {u[:4]}",
                subscriber_count=len(subscribers))
    session.add(feed)
    await session.flush()
    for user in subscribers:
        session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return feed


async def _article(session, feed, *, story_id=None, title="T", minutes_ago=0,
                   trimmed=False) -> Article:
    u = uuid.uuid4().hex
    article = Article(
        feed_id=feed.id if feed else None, guid=u, guid_hash=u, title=title,
        story_id=story_id,
        published_at=NOW - timedelta(minutes=minutes_ago),
        fetched_at=NOW - timedelta(minutes=minutes_ago),
        trimmed_at=NOW if trimmed else None,
    )
    session.add(article)
    await session.flush()
    return article


async def _state(session, user, article, **kwargs) -> UserArticleState:
    state = UserArticleState(user_id=user.id, article_id=article.id, **kwargs)
    session.add(state)
    await session.flush()
    return state


async def _story(session, user, *, extra_feeds=0):
    """A three-article story: two in a feed the user reads, one in a feed they don't."""
    mine = await _feed(session, user)
    theirs = await _feed(session)
    head = await _article(session, mine, title="Head", minutes_ago=30)
    head.story_id = head.id
    await session.flush()
    await _article(session, mine, story_id=head.id, title="Mine too", minutes_ago=20)
    await _article(session, theirs, story_id=head.id, title="Not subscribed", minutes_ago=10)
    return head, mine, theirs


class TestAccess:
    async def test_a_feed_the_reader_does_not_take_stays_out(self, pg):
        user = await _user(pg)
        head, _, _ = await _story(pg, user)

        members = await list_members(user.id, head.story_id, head.id, pg)

        assert [m.title for m in members] == ["Mine too"]

    async def test_count_agrees_with_the_list(self, pg):
        user = await _user(pg)
        head, _, _ = await _story(pg, user)

        assert await count_members(user.id, head.story_id, head.id, pg) == 1

    async def test_a_starred_article_survives_unsubscribing(self, pg):
        """Same rule as everywhere else: a star keeps access after the feed is gone."""
        user = await _user(pg)
        head, _, theirs = await _story(pg, user)
        outsider = (await list_members(user.id, head.story_id, head.id, pg))
        assert len(outsider) == 1  # precondition: the other feed is invisible

        not_subscribed = await _article(pg, theirs, story_id=head.story_id, title="Starred")
        await _state(pg, user, not_subscribed, is_starred=True)

        titles = [m.title for m in await list_members(user.id, head.story_id, head.id, pg)]
        assert "Starred" in titles

    async def test_a_stranger_sees_nothing_of_the_group(self, pg):
        user = await _user(pg)
        stranger = await _user(pg)
        head, _, _ = await _story(pg, user)

        assert await count_members(stranger.id, head.story_id, head.id, pg) == 0
        assert await list_members(stranger.id, head.story_id, head.id, pg) == []


class TestContent:
    async def test_no_story_means_no_query(self, pg):
        user = await _user(pg)
        assert await count_members(user.id, None, 1, pg) == 0
        assert await list_members(user.id, None, 1, pg) == []

    async def test_the_article_being_read_is_not_its_own_related(self, pg):
        user = await _user(pg)
        head, _, _ = await _story(pg, user)

        assert head.id not in [m.id for m in await list_members(user.id, head.story_id, head.id, pg)]

    async def test_trimmed_articles_are_left_out(self, pg):
        """Retention stripped them to a snippet; they are gone from every other view."""
        user = await _user(pg)
        head, mine, _ = await _story(pg, user)
        await _article(pg, mine, story_id=head.story_id, title="Trimmed", trimmed=True)

        titles = [m.title for m in await list_members(user.id, head.story_id, head.id, pg)]
        assert "Trimmed" not in titles

    async def test_read_and_starred_members_are_still_listed(self, pg):
        """The footer exists to surface coverage; filtering by state would defeat it."""
        user = await _user(pg)
        head, mine, _ = await _story(pg, user)
        already_read = await _article(pg, mine, story_id=head.story_id, title="Read one")
        await _state(pg, user, already_read, is_read=True)

        members = await list_members(user.id, head.story_id, head.id, pg)
        read_member = next(m for m in members if m.title == "Read one")
        assert read_member.is_read is True

    async def test_newest_first(self, pg):
        user = await _user(pg)
        mine = await _feed(pg, user)
        head = await _article(pg, mine, title="Head", minutes_ago=60)
        head.story_id = head.id
        await pg.flush()
        await _article(pg, mine, story_id=head.id, title="Older", minutes_ago=50)
        await _article(pg, mine, story_id=head.id, title="Newer", minutes_ago=5)

        titles = [m.title for m in await list_members(user.id, head.id, head.id, pg)]
        assert titles == ["Newer", "Older"]

    async def test_custom_feed_title_wins(self, pg):
        user = await _user(pg)
        mine = await _feed(pg, user)
        head = await _article(pg, mine, title="Head", minutes_ago=30)
        head.story_id = head.id
        await pg.flush()
        await _article(pg, mine, story_id=head.id, title="Other")
        subscription = await pg.scalar(
            select(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == mine.id)
        )
        subscription.custom_title = "My name for it"
        await pg.flush()

        members = await list_members(user.id, head.id, head.id, pg)
        assert members[0].feed_title == "My name for it"

    async def test_a_pathological_group_is_capped(self, pg):
        user = await _user(pg)
        mine = await _feed(pg, user)
        head = await _article(pg, mine, title="Head", minutes_ago=60)
        head.story_id = head.id
        await pg.flush()
        for i in range(MEMBER_LIMIT + 5):
            await _article(pg, mine, story_id=head.id, title=f"M{i}", minutes_ago=i)

        assert len(await list_members(user.id, head.id, head.id, pg)) == MEMBER_LIMIT

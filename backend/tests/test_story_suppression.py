"""``suppressed_at``: who marked the article read, the reader or the machine.

The column is what the suppression rule stands on. An article is only hidden for
resembling one the reader has seen, and "seen" means a read the reader made
themselves, so a read written by the URL dedup, a filter action or the closing of a
story must never pass for one. That only holds if the mark is also taken off again the
moment the reader does read the article, otherwise a machine mark from weeks ago sticks
to an article they have since read properly and it silently stops counting.

So: every human way of setting ``is_read`` clears it, and so does any real sign of the
reader working with the article (half a minute in front of it, opening the link,
starring it). Errors here lean one way on purpose — a stamp wrongly cleared shows the
reader more, a stamp wrongly kept hides an article they had already seen.

Runs against the real database inside a transaction that is always rolled back.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.schemas.article import ArticleStateUpdate
from app.services.article import (
    mark_articles_read_batch,
    mark_scope_read,
    toggle_article_state,
    update_article_state,
)

NOW = datetime.now(timezone.utc)
LONG_AGO = NOW - timedelta(days=3)


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
    user = User(email=f"supp_{u}@test.invalid", password_hash="x", display_name="t")
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
                      published_at=LONG_AGO, fetched_at=LONG_AGO)
    pg.add(article)
    await pg.flush()
    return article


async def _machine_read(pg, user, article) -> UserArticleState:
    """The state a filter action or a closed story leaves behind."""
    state = UserArticleState(user_id=user.id, article_id=article.id, is_read=True,
                             read_at=LONG_AGO, suppressed_at=LONG_AGO)
    pg.add(state)
    await pg.flush()
    return state


async def _unread(pg, user, article) -> UserArticleState:
    """What the reader is left with after taking a machine read back: unread, but the
    stamp is still on the row. This is the sequence that made the fix necessary."""
    state = UserArticleState(user_id=user.id, article_id=article.id, is_read=False,
                             read_at=None, suppressed_at=LONG_AGO)
    pg.add(state)
    await pg.flush()
    return state


async def _stamp(pg, user, article):
    """Read the column straight from the database, not off the session's copy."""
    return await pg.scalar(
        select(UserArticleState.suppressed_at).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article.id,
        )
    )


class TestAHumanReadClearsTheMark:
    async def test_the_scroll_batch_clears_it(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _unread(pg, user, article)

        await mark_articles_read_batch(user, [article.id], pg)

        assert await _stamp(pg, user, article) is None

    async def test_the_read_button_clears_it(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _unread(pg, user, article)

        await toggle_article_state(user, article.id, "is_read", pg)

        assert await _stamp(pg, user, article) is None

    async def test_setting_read_over_the_api_clears_it(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _unread(pg, user, article)

        await update_article_state(user, article.id, ArticleStateUpdate(is_read=True), pg)

        assert await _stamp(pg, user, article) is None

    async def test_taking_a_machine_read_back_clears_it(self, pg):
        """Unread and still stamped is a state nothing should be in: it is the one that
        later lets a real read pass for a machine one."""
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await toggle_article_state(user, article.id, "is_read", pg)

        assert await _stamp(pg, user, article) is None

    async def test_mark_all_read_clears_it_on_what_it_flips(self, pg):
        user = await _user(pg)
        feed = await _feed(pg, user)
        article = await _article(pg, feed)
        await _unread(pg, user, article)

        await mark_scope_read(user, pg, before=NOW, feed_id=feed.id)

        assert await _stamp(pg, user, article) is None

    async def test_mark_all_read_does_not_promote_a_machine_read(self, pg):
        """Clearing the deck is not reading. The article was already read, so mark all
        read passes over it, and it must stay a machine read — nothing new was seen."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        article = await _article(pg, feed)
        await _machine_read(pg, user, article)

        await mark_scope_read(user, pg, before=NOW, feed_id=feed.id)

        assert await _stamp(pg, user, article) == LONG_AGO

    async def test_a_plain_read_is_not_stamped(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))

        await mark_articles_read_batch(user, [article.id], pg)

        assert await _stamp(pg, user, article) is None


class TestWorkingWithTheArticleClearsTheMark:
    """The reader can reach a closed article from the footer of the one that closed it.
    If they then read it, the machine's guess about this article was wrong and the
    stamp has to go, or the article keeps being a nobody for the suppression rule."""

    async def _dwell(self, pg, user, article, seconds):
        from app.routers.web.app.articles import htmx_article_dwell
        await htmx_article_dwell(article.id, seconds=seconds, user=user, db=pg)

    async def test_half_a_minute_in_front_of_it_clears_it(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await self._dwell(pg, user, article, 45)

        assert await _stamp(pg, user, article) is None

    async def test_a_glance_does_not(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await self._dwell(pg, user, article, 10)

        assert await _stamp(pg, user, article) == LONG_AGO

    async def test_two_glances_that_add_up_do(self, pg):
        """Dwell arrives in pieces, so the threshold is on the total, not on one ping."""
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await self._dwell(pg, user, article, 20)
        await self._dwell(pg, user, article, 20)

        assert await _stamp(pg, user, article) is None

    async def test_opening_the_link_clears_it(self, pg):
        from app.routers.web.app.articles import htmx_article_link_opened
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await htmx_article_link_opened(article.id, user=user, db=pg)

        assert await _stamp(pg, user, article) is None

    async def test_starring_it_clears_it(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await toggle_article_state(user, article.id, "is_starred", pg)

        assert await _stamp(pg, user, article) is None

    async def test_unstarring_does_not_put_it_back(self, pg):
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        await _machine_read(pg, user, article)

        await toggle_article_state(user, article.id, "is_starred", pg)
        await toggle_article_state(user, article.id, "is_starred", pg)

        assert await _stamp(pg, user, article) is None

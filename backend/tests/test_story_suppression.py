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
from app.fetcher.stories import assign_stories, assign_stories_global
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User, UserSettings
from app.schemas.article import ArticleStateUpdate
from app.services.article import (
    mark_articles_read_batch,
    mark_scope_read,
    toggle_article_state,
    update_article_state,
)
from app.services.story_service import DEDUP_COLLAPSE, DEDUP_OFF, DEDUP_SUPPRESS

NOW = datetime.now(timezone.utc)
LONG_AGO = NOW - timedelta(days=3)


@pytest_asyncio.fixture
def nonce():
    """Short token shared by one test's titles, so they can only ever match each other
    and never a real article sitting in the development database. Short because it
    lands in every title, and a long one would lift the scores the tests turn on."""
    return "zz" + uuid.uuid4().hex[:4]


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


async def _feed(pg, *subscribers) -> Feed:
    u = uuid.uuid4().hex[:12]
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"Feed {u[:4]}",
                subscriber_count=len(subscribers))
    pg.add(feed)
    await pg.flush()
    for user in subscribers:
        pg.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await pg.flush()
    return feed


async def _article(pg, feed, title="T", *, hours_ago=0) -> Article:
    u = uuid.uuid4().hex
    when = NOW - timedelta(hours=hours_ago) if hours_ago else LONG_AGO
    article = Article(feed_id=feed.id, guid=u, guid_hash=u, title=title,
                      published_at=when, fetched_at=when)
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


# ── the rule itself: hiding a repeat of something already read ────────────────

# Measured against the live pg_trgm: 0.86, well over the 0.40 that hiding asks for.
SEEN_TITLE = "city council approves the new tram line"
REPEAT_TITLE = "new tram line approved by the city council"
# 0.35: enough to fold the two together in the list, not enough to take one away.
RELATED_TITLE = "new tram line to open next year"


async def _settings(pg, user, mode) -> UserSettings:
    s = UserSettings(user_id=user.id, story_dedup=mode)
    pg.add(s)
    await pg.flush()
    return s


async def _read_by_hand(pg, user, article) -> UserArticleState:
    state = UserArticleState(user_id=user.id, article_id=article.id, is_read=True,
                             read_at=NOW - timedelta(hours=2))
    pg.add(state)
    await pg.flush()
    return state


async def _story_of(pg, article) -> int | None:
    """From the database: the grouping is done in SQL, so the session's copy is stale."""
    return await pg.scalar(select(Article.story_id).where(Article.id == article.id))


async def _state(pg, user, article) -> UserArticleState | None:
    return await pg.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article.id,
        )
    )


async def _arrives(pg, user, title=REPEAT_TITLE, *, nonce="", feed=None):
    """A new article lands in a feed the reader takes, and the fetcher groups it."""
    feed = feed if feed is not None else await _feed(pg, user)
    article = await _article(pg, feed, f"{nonce} {title}".strip(), hours_ago=1)
    await assign_stories([article], pg)
    return article


class TestHidingARepeat:
    """The opt-in half of story dedup: an article that repeats one the reader has
    already read does not come back as a new row.

    Every one of these needs the real pg_trgm, so the titles are fixed phrases whose
    scores were measured rather than assumed, and the nonce keeps them from pairing up
    with whatever else is in the development database.
    """

    async def test_a_repeat_of_something_read_is_hidden(self, pg, nonce):
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, nonce=nonce)

        state = await _state(pg, user, arrival)
        assert state is not None and state.is_read is True
        assert state.suppressed_by == "similar"

    async def test_folding_alone_hides_nothing(self, pg, nonce):
        """The default. The article is still grouped, it just stays in the list."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_COLLAPSE)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, nonce=nonce)

        assert await _state(pg, user, arrival) is None
        assert await _story_of(pg, arrival) == seen.id

    async def test_the_feature_off_hides_nothing(self, pg, nonce):
        user = await _user(pg)
        await _settings(pg, user, DEDUP_OFF)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, nonce=nonce)

        assert await _state(pg, user, arrival) is None

    async def test_a_match_that_only_folds_is_not_strong_enough_to_hide(self, pg, nonce):
        """0.30 folds, 0.40 hides. Group membership is transitive and reaches further
        than either, which is why hiding is decided on the pair and not on the group."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, RELATED_TITLE, nonce=nonce)

        assert await _story_of(pg, arrival) == seen.id
        assert await _state(pg, user, arrival) is None

    async def test_a_machine_read_cannot_hide_anything(self, pg, nonce):
        """Otherwise one automatic decision feeds the next: a filter closes an article,
        that closes the next day's coverage, and the reader never sees the story."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _machine_read(pg, user, seen)

        arrival = await _arrives(pg, user, nonce=nonce)

        assert await _state(pg, user, arrival) is None

    async def test_an_unread_article_hides_nothing(self, pg, nonce):
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)

        arrival = await _arrives(pg, user, nonce=nonce)

        assert await _state(pg, user, arrival) is None

    async def test_an_article_a_filter_starred_is_left_alone(self, pg, nonce):
        """A star is the reader saying in advance that they want this one, and filters
        run before the grouping does, so the state row is already there."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        feed = await _feed(pg, user)
        arrival = await _article(pg, feed, f"{nonce} {REPEAT_TITLE}", hours_ago=1)
        pg.add(UserArticleState(user_id=user.id, article_id=arrival.id, is_starred=True))
        await pg.flush()

        await assign_stories([arrival], pg)

        state = await _state(pg, user, arrival)
        assert state.is_read is False
        assert state.suppressed_at is None

    async def test_the_window_holds(self, pg, nonce):
        """Coverage a week apart is the next story about the same subject, not a repeat
        of this one."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=200)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, nonce=nonce)

        assert await _story_of(pg, arrival) is None
        assert await _state(pg, user, arrival) is None

    async def test_only_the_reader_who_read_it_loses_the_article(self, pg, nonce):
        """The grouping is global, the reading is not."""
        user = await _user(pg)
        stranger = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        await _settings(pg, stranger, DEDUP_SUPPRESS)
        source = await _feed(pg, user, stranger)
        seen = await _article(pg, source, f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _arrives(pg, user, nonce=nonce, feed=await _feed(pg, user, stranger))

        assert (await _state(pg, user, arrival)).suppressed_by == "similar"
        assert await _state(pg, stranger, arrival) is None

    async def test_a_reader_who_does_not_take_the_new_feed_is_not_touched(self, pg, nonce):
        """Nothing to hide from someone the article was never going to reach; writing a
        state row for them would only leave rubbish behind."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        elsewhere = await _feed(pg)
        arrival = await _article(pg, elsewhere, f"{nonce} {REPEAT_TITLE}", hours_ago=1)
        await assign_stories([arrival], pg)

        assert await _state(pg, user, arrival) is None

    async def test_the_post_gather_pass_hides_it_too(self, pg, nonce):
        """Two feeds in one scheduler round cannot see each other's rows, so the second
        pass is where most cross-feed pairs are actually found."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _article(pg, await _feed(pg, user), f"{nonce} {REPEAT_TITLE}",
                                 hours_ago=1)

        await assign_stories_global(NOW - timedelta(hours=2), pg)

        assert (await _state(pg, user, arrival)).suppressed_by == "similar"

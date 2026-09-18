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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher.stories import (
    assign_stories,
    assign_stories_global,
    reads_as_follow_up,
)
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
from app.services.story_service import (
    DEDUP_COLLAPSE,
    DEDUP_OFF,
    DEDUP_SUPPRESS,
    SUPPRESSED_BY_STORY,
    count_suppressed,
    list_suppressed,
)

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

    async def test_mark_all_read_does_not_promote_a_machine_read(self, pg):
        """Clearing the deck is not reading. The article was already read, so mark all
        read passes over it, and it must stay a machine read — nothing new was seen."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        article = await _article(pg, feed)
        await _machine_read(pg, user, article)

        await mark_scope_read(user, pg, before=NOW, feed_id=feed.id)

        assert await _stamp(pg, user, article) == LONG_AGO


class TestMarkAllReadIsNotReading:
    """Clearing a backlog says the reader will not read these, not that they have.

    Without the stamp one press over a few hundred articles would arm the suppression
    against everything they were about, and the reader would spend the following days
    not being shown news they never saw in the first place.
    """

    async def _reason(self, pg, user, article):
        return await pg.scalar(
            select(UserArticleState.suppressed_by).where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id == article.id,
            )
        )

    async def test_an_article_it_flips_is_stamped(self, pg):
        user = await _user(pg)
        feed = await _feed(pg, user)
        article = await _article(pg, feed)

        await mark_scope_read(user, pg, before=NOW, feed_id=feed.id)

        assert await _stamp(pg, user, article) is not None
        assert await self._reason(pg, user, article) == "bulk"

    async def test_it_is_stamped_over_an_existing_row_too(self, pg):
        """The state row already being there (unread, previously stamped) is the same
        gesture and must not come out of it looking like a read."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        article = await _article(pg, feed)
        await _unread(pg, user, article)

        await mark_scope_read(user, pg, before=NOW, feed_id=feed.id)

        assert await self._reason(pg, user, article) == "bulk"

    async def test_the_starred_scope_stamps_as_well(self, pg):
        """That branch is a plain UPDATE rather than an upsert, so it is a second
        place the stamp has to be written."""
        user = await _user(pg)
        article = await _article(pg, await _feed(pg, user))
        pg.add(UserArticleState(user_id=user.id, article_id=article.id,
                                is_read=False, is_starred=True))
        await pg.flush()

        await mark_scope_read(user, pg, before=NOW, starred_only=True)

        assert await self._reason(pg, user, article) == "bulk"

    async def test_reading_one_afterwards_takes_the_stamp_off(self, pg):
        """The stamp withholds a signal that was never given; it does not stand in the
        way of one that is."""
        user = await _user(pg)
        feed = await _feed(pg, user)
        article = await _article(pg, feed)
        await mark_scope_read(user, pg, before=NOW, feed_id=feed.id)

        await toggle_article_state(user, article.id, "is_read", pg)   # unread
        await toggle_article_state(user, article.id, "is_read", pg)   # read, by hand

        assert await _stamp(pg, user, article) is None

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
# 0.82, so the score is not what stops this one: the "why" is. An explainer written off
# the back of the report is the same news and a different piece of writing.
FOLLOW_UP_TITLE = "why the city council approved the new tram line"
# Both of these ask "how", so the word is not what tells them apart (0.78).
HOW_SEEN_TITLE = "how the city council approves the new tram line"
HOW_REPEAT_TITLE = "how the new tram line was approved by the city council"


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


class TestAFollowUpIsNotARepeat:
    """An explainer or a second actor doing the same thing covers the news the reader
    read and is still something they have not read.

    The cue words were measured, not invented: a wide list of framing words was tried
    against a labelled corpus and against our own, and it cost more right decisions than
    it saved. What survived is the short list of words that carry the framing themselves.
    See scripts/BENCHMARKS.md.
    """

    def test_a_cue_on_one_side_reads_as_a_follow_up(self):
        assert reads_as_follow_up("why the tram line was approved",
                                  "the tram line was approved") is True
        assert reads_as_follow_up("apple maps joins google maps in renaming the lake",
                                  "google maps renames the lake") is True

    def test_the_same_cue_on_both_sides_tells_them_apart_from_nothing(self):
        assert reads_as_follow_up("how to watch the launch",
                                  "how to watch tonight's launch") is False

    def test_a_plain_pair_carries_no_cue(self):
        assert reads_as_follow_up("city council approves the tram line",
                                  "tram line approved by the city council") is False

    async def test_an_explainer_is_grouped_but_not_hidden(self, pg, nonce):
        """0.82 against the article they read, so the score is not what stops it."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, FOLLOW_UP_TITLE, nonce=nonce)

        assert await _story_of(pg, arrival) == seen.id
        assert await _state(pg, user, arrival) is None

    async def test_two_explainers_still_hide_each_other(self, pg, nonce):
        """The cue only means something when it is what distinguishes the two."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {HOW_SEEN_TITLE}",
                              hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, HOW_REPEAT_TITLE, nonce=nonce)

        assert (await _state(pg, user, arrival)).suppressed_by == "similar"


class TestTheListOfWhatWasHidden:
    """Settings shows how many articles were hidden; this is what is behind the number.

    The list is the only account the reader gets of what the third setting did on their
    behalf, so it has to hold to the same two rules the counter does: only what the
    similarity rule hid, and only what the reader could still open. It also has to say
    what each article was hidden for, and that is not stored anywhere — it is found
    again from the story the two share, which is what most of these are about.
    """

    async def _hidden(self, pg, user):
        return await list_suppressed(user.id, pg)

    async def test_a_hidden_article_says_what_it_lost_out_to(self, pg, nonce):
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)

        arrival = await _arrives(pg, user, nonce=nonce)

        hidden = await self._hidden(pg, user)
        assert [h.id for h in hidden] == [arrival.id]
        assert hidden[0].instead_of == seen.title
        assert hidden[0].match >= 0.40
        assert hidden[0].still_hidden is True
        assert await count_suppressed(user.id, pg) == 1

    async def test_the_other_machine_reads_are_not_in_it(self, pg, nonce):
        """The column carries four kinds of machine read and the setting is about one.
        A story closed behind the reader is not something that was kept from them."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        article = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}")
        state = await _machine_read(pg, user, article)
        state.suppressed_by = SUPPRESSED_BY_STORY
        await pg.flush()

        assert await self._hidden(pg, user) == []
        assert await count_suppressed(user.id, pg) == 0

    async def test_the_window_ends_it(self, pg, nonce):
        """Seven days, the same seven the counter above the list is made of."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _arrives(pg, user, nonce=nonce)

        state = await _state(pg, user, arrival)
        state.hidden_at = NOW - timedelta(days=8)
        await pg.flush()

        assert await self._hidden(pg, user) == []
        assert await count_suppressed(user.id, pg) == 0

    async def test_reading_one_leaves_the_row_and_says_so(self, pg, nonce):
        """The whole reason the record has a column of its own.

        Checking whether a call was right means opening the article, and opening it is
        reading it, which is what lifts the hiding. On the live columns the row would
        take itself off the list and off the counter at exactly the moment the reader
        went to look at it — the wrong ones most of all, since those are the ones worth
        opening.
        """
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _arrives(pg, user, nonce=nonce)

        # The reader opens it from the list and reads it, which clears the stamp the
        # decision stands on (half a minute in front of it does the same).
        await update_article_state(user, arrival.id, ArticleStateUpdate(is_read=True), pg)

        assert await _stamp(pg, user, arrival) is None
        hidden = await self._hidden(pg, user)
        assert [h.id for h in hidden] == [arrival.id]
        assert hidden[0].still_hidden is False
        assert await count_suppressed(user.id, pg) == 1

    async def test_unsubscribing_takes_the_article_out_of_the_list(self, pg, nonce):
        """The list goes through the same access gate as every other read path, so a
        feed the reader has dropped takes what it hid with it. The counter is built on
        the same query, which is what keeps the number and the list saying one thing."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        feed = await _feed(pg, user)
        await _arrives(pg, user, nonce=nonce, feed=feed)
        assert await count_suppressed(user.id, pg) == 1

        await pg.execute(
            delete(UserFeed).where(UserFeed.user_id == user.id, UserFeed.feed_id == feed.id)
        )

        assert await self._hidden(pg, user) == []
        assert await count_suppressed(user.id, pg) == 0

    async def test_the_reason_is_left_out_when_that_article_is_gone(self, pg, nonce):
        """The row stands on its own: the article was hidden, and the counter counts it,
        whether or not the article it lost out to can still be pointed at."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen_feed = await _feed(pg, user)
        seen = await _article(pg, seen_feed, f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _arrives(pg, user, nonce=nonce)

        await pg.execute(
            delete(UserFeed).where(
                UserFeed.user_id == user.id, UserFeed.feed_id == seen_feed.id
            )
        )

        hidden = await self._hidden(pg, user)
        assert [h.id for h in hidden] == [arrival.id]
        assert hidden[0].instead_of is None
        assert hidden[0].match is None

    async def test_a_read_the_reader_did_not_make_is_not_offered_as_the_reason(self, pg, nonce):
        """Only a read the reader made themselves can hide anything, so only one can be
        the reason. A member closed on their behalf is read, and explains nothing.

        The one that must not win is the closer match of the two (0.89 against 0.84), so
        this fails if the question ever quietly becomes "which is most alike"."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _arrives(pg, user, nonce=nonce)
        # A third article in the same story, hidden in its turn. It is the closest match
        # to the second of the three, and being read is still not an answer for it.
        await _arrives(pg, user, f"{REPEAT_TITLE} today", nonce=nonce)

        hidden = {h.id: h for h in await self._hidden(pg, user)}
        assert len(hidden) == 2
        assert hidden[arrival.id].instead_of == seen.title

    async def test_an_explainer_is_not_offered_as_the_reason(self, pg, nonce):
        """Same rule as the hiding itself: a headline the rule would never have hidden
        anything for cannot be why this one went.

        The explainer is the closer match here too (0.84 against 0.84, by a hair), which
        is what makes the cue and not the score the thing being tested."""
        user = await _user(pg)
        await _settings(pg, user, DEDUP_SUPPRESS)
        seen = await _article(pg, await _feed(pg, user), f"{nonce} {SEEN_TITLE}", hours_ago=5)
        await _read_by_hand(pg, user, seen)
        arrival = await _arrives(pg, user, nonce=nonce)
        explainer = await _article(pg, await _feed(pg, user), f"{nonce} {FOLLOW_UP_TITLE}",
                                   hours_ago=2)
        await assign_stories([explainer], pg)
        await _read_by_hand(pg, user, explainer)

        hidden = await self._hidden(pg, user)
        assert hidden[0].instead_of == seen.title

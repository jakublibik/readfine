"""Story grouping as the article list uses it: folding a group into one row and
saying what is behind that row.

Two things have to hold. The fold must keep the page in order and never swallow a row
that is not part of a group, because a swallowed row is a lost article. And the numbers
on the row are user-scoped, so they must count only what this reader may open — the
grouping itself is global and routinely reaches into feeds nobody here subscribes to.

The counting half runs against the real database inside a transaction that is always
rolled back.
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
from app.schemas.article import ArticleListItem
from app.services.article import (
    list_articles,
    mark_articles_read_batch,
    toggle_article_state,
    update_article_state,
)
from app.services.story_service import (
    DEDUP_COLLAPSE,
    DEDUP_OFF,
    DEDUP_SUPPRESS,
    MAX_SHOWN_STORIES,
    row_count,
    annotate,
    collapse_page,
    mark_group_read,
    next_shown,
    parse_shown,
)

NOW = datetime.now(timezone.utc)


def _item(article_id: int, *, story_id=None, is_read=False) -> ArticleListItem:
    return ArticleListItem(
        id=article_id, feed_id=1, feed_title="F", url=None, title=f"A{article_id}",
        author=None, summary=None, snippet=None, published_at=None,
        formatted_date="", estimated_read_min=None, image_url=None,
        is_read=is_read, is_starred=False, is_archived=False, story_id=story_id,
    )


class TestCollapsePage:
    def test_the_first_row_of_a_group_stands_for_it(self):
        page = [_item(1, story_id=7), _item(2, story_id=7), _item(3, story_id=7)]
        assert [i.id for i in collapse_page(page)] == [1]

    def test_rows_without_a_story_are_never_folded(self):
        """Including several of them: no story is not a group of its own."""
        page = [_item(1), _item(2), _item(3)]
        assert [i.id for i in collapse_page(page)] == [1, 2, 3]

    def test_order_and_the_rest_of_the_page_survive(self):
        page = [
            _item(1), _item(2, story_id=7), _item(3), _item(4, story_id=7),
            _item(5, story_id=9), _item(6),
        ]
        assert [i.id for i in collapse_page(page)] == [1, 2, 3, 5, 6]

    def test_groups_are_independent(self):
        page = [_item(1, story_id=7), _item(2, story_id=9), _item(3, story_id=7)]
        assert [i.id for i in collapse_page(page)] == [1, 2]

    def test_empty_page(self):
        assert collapse_page([]) == []


class TestGroupsSplitByThePageBoundary:
    """A group whose members fall on either side of a page boundary is still one
    group: the pages below are told which stories already have a row above them."""

    def test_a_member_of_a_story_shown_earlier_is_dropped(self):
        page = [_item(1), _item(2, story_id=7), _item(3)]
        assert [i.id for i in collapse_page(page, [7])] == [1, 3]

    def test_a_story_not_shown_yet_still_gets_its_row(self):
        page = [_item(1, story_id=9), _item(2, story_id=9)]
        assert [i.id for i in collapse_page(page, [7])] == [1]

    def test_the_next_page_is_told_about_this_one(self):
        page = [_item(1, story_id=9), _item(2), _item(3, story_id=11)]
        assert next_shown([7], page) == [7, 9, 11]

    def test_a_story_is_listed_once(self):
        assert next_shown([7], [_item(1, story_id=7)]) == [7]

    def test_the_list_is_capped_and_keeps_the_recent_end(self):
        """Overflow drops the oldest, which are far outside the 72 hour window a
        later page can still reach into."""
        long_list = list(range(MAX_SHOWN_STORIES + 10))
        assert next_shown(long_list, [_item(1, story_id=999)])[-1] == 999
        assert len(next_shown(long_list, [_item(1, story_id=999)])) == MAX_SHOWN_STORIES

    def test_nothing_shown_yet(self):
        assert next_shown([], [_item(1), _item(2)]) == []


class TestParseShown:
    def test_reads_the_list_back(self):
        assert parse_shown("7,9,11") == [7, 9, 11]

    def test_missing_or_empty_is_nothing_shown(self):
        assert parse_shown(None) == []
        assert parse_shown("") == []

    def test_junk_is_dropped_rather_than_raised(self):
        """It arrives from the client, and a broken value must cost a row at worst,
        never the page."""
        assert parse_shown("7,,x,9,10") == [7, 9, 10]
        assert parse_shown("7,9;9,10") == [7, 10]  # not a number, not a separator

    def test_the_length_is_capped(self):
        raw = ",".join(str(i) for i in range(MAX_SHOWN_STORIES + 50))
        assert len(parse_shown(raw)) == MAX_SHOWN_STORIES


class TestWhichViewsFold:
    """Which views fold a story into one row, which is the same answer as whether a
    row there may offer to unfold one.

    One rule, two consequences, which is why it is worth pinning down. A view that
    folds hid something, so it can give it back, and its members are guaranteed not to
    be in the list already. A view that folds nothing has neither property: unfolding
    would push articles from other feeds into a list the reader assembled by hand or
    opened to see one feed, and those rows mark themselves read on scroll.
    """

    def _folds(self, **kw):
        from app.routers.web.app.articles import _collapses_stories
        opts = {"story_dedup": DEDUP_COLLAPSE, "feed_id": None, "starred_only": False,
                "archived_only": False, "saved_only": False}
        return _collapses_stories(**{**opts, **kw})

    def test_the_reading_views_fold(self):
        """All articles, a folder and a label all arrive here with nothing set."""
        assert self._folds() is True

    def test_a_single_feed_does_not(self):
        assert self._folds(feed_id=7) is False

    def test_the_hand_assembled_lists_do_not(self):
        assert self._folds(starred_only=True) is False
        assert self._folds(archived_only=True) is False
        assert self._folds(saved_only=True) is False

    def test_the_setting_overrules_the_view(self):
        """Off means the list looks like it did before any of this existed."""
        assert self._folds(story_dedup=DEDUP_OFF) is False
        assert self._folds(story_dedup=DEDUP_SUPPRESS) is True


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
    user = User(email=f"list_{u}@test.invalid", password_hash="x", display_name="t")
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


async def _story(session, user):
    """Three articles on one story: two in a feed the user takes, one in a feed
    they do not."""
    mine = await _feed(session, user)
    theirs = await _feed(session)
    head = await _article(session, mine, title="Head", minutes_ago=30)
    head.story_id = head.id
    await session.flush()
    second = await _article(session, mine, story_id=head.id, title="Mine too",
                            minutes_ago=20)
    await _article(session, theirs, story_id=head.id, title="Not subscribed",
                   minutes_ago=10)
    return head, second, mine, theirs


class TestAnnotate:
    async def test_counts_only_what_the_reader_can_open(self, pg):
        user = await _user(pg)
        head, _, _, _ = await _story(pg, user)

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg)

        # Three in the group, one of them in a feed this reader does not take.
        assert row.story_others == 1

    async def test_the_row_does_not_count_itself_as_read(self, pg):
        """Otherwise every read row would claim the reader had seen a sibling."""
        user = await _user(pg)
        head, _, _, _ = await _story(pg, user)
        pg.add(UserArticleState(user_id=user.id, article_id=head.id, is_read=True))
        await pg.flush()

        row = _item(head.id, story_id=head.story_id, is_read=True)
        await annotate([row], user.id, pg)

        assert row.story_read == 0

    async def test_a_read_sibling_marks_the_row(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        pg.add(UserArticleState(user_id=user.id, article_id=second.id, is_read=True))
        await pg.flush()

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg)

        assert row.story_read == 1

    async def test_a_read_sibling_in_a_feed_the_reader_lost_does_not_count(self, pg):
        """Same gate as the count: a member behind a subscription the reader does not
        have is not theirs to have read."""
        user = await _user(pg)
        stranger = await _user(pg)
        head, _, _, theirs = await _story(pg, user)
        outsider = await _article(pg, theirs, story_id=head.story_id, title="Theirs")
        pg.add(UserArticleState(user_id=stranger.id, article_id=outsider.id, is_read=True))
        await pg.flush()

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg)

        assert row.story_read == 0
        assert row.story_others == 1

    async def test_trimmed_members_are_not_counted(self, pg):
        """Retention stripped them to stubs and they are gone from every view, so a
        row must not promise one."""
        user = await _user(pg)
        head, _, mine, _ = await _story(pg, user)
        await _article(pg, mine, story_id=head.story_id, title="Old", trimmed=True)

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg)

        assert row.story_others == 1

    async def test_a_stranger_is_told_nothing(self, pg):
        user = await _user(pg)
        stranger = await _user(pg)
        head, _, _, _ = await _story(pg, user)

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], stranger.id, pg)

        assert row.story_others == 0
        assert row.story_read == 0

    async def test_a_sibling_closed_on_the_readers_behalf_does_not_count(self, pg):
        """Finishing a story marks the rest of it read, and counting those would make
        the badge say all of them the moment the reader opened one article."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        pg.add(UserArticleState(
            user_id=user.id, article_id=second.id, is_read=True,
            suppressed_at=NOW, suppressed_by="story",
        ))
        await pg.flush()

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg)

        assert row.story_read == 0

    async def test_a_row_the_machine_closed_takes_nothing_off_the_count(self, pg):
        """The row subtracts itself only when it is one of the reads being counted.
        Here it is not, so the sibling the reader did read has to survive it."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        pg.add(UserArticleState(
            user_id=user.id, article_id=head.id, is_read=True,
            suppressed_at=NOW, suppressed_by="story",
        ))
        pg.add(UserArticleState(user_id=user.id, article_id=second.id, is_read=True))
        await pg.flush()

        row = _item(head.id, story_id=head.story_id, is_read=True)
        await annotate([row], user.id, pg)

        assert row.story_read == 1

    async def test_rows_without_a_story_are_left_alone(self, pg):
        user = await _user(pg)
        rows = [_item(1), _item(2)]

        await annotate(rows, user.id, pg)

        assert all(r.story_others == 0 and r.story_read == 0 for r in rows)

    async def test_every_row_of_one_page_is_answered_together(self, pg):
        """The page is one query, so two groups on it must both come back right."""
        user = await _user(pg)
        head_a, _, mine, _ = await _story(pg, user)
        head_b = await _article(pg, mine, title="Other head")
        head_b.story_id = head_b.id
        await pg.flush()
        await _article(pg, mine, story_id=head_b.id, title="Other second")
        await _article(pg, mine, story_id=head_b.id, title="Other third")

        rows = [
            _item(head_a.id, story_id=head_a.story_id),
            _item(head_b.id, story_id=head_b.story_id),
        ]
        await annotate(rows, user.id, pg)

        assert [r.story_others for r in rows] == [1, 2]


class TestScopedCounts:
    """What a filtered list promises must be what unfolding gives back.

    A label list folds the members carrying that label, so those are the ones it may
    unfold. Handing back the rest would put articles in the list that were never in it,
    and since unfolded rows mark themselves read on scroll, it would quietly take them
    off the reader's unread list too.

    The group is still worth naming in full, which is why the row carries both numbers:
    one article caught by a label can be part of something much bigger, and that is a
    fact about the news rather than about the filter.
    """

    async def test_scope_splits_the_two_numbers(self, pg):
        user = await _user(pg)
        head, second, mine, _ = await _story(pg, user)
        # A third member the reader can open, so that the filter has something to
        # exclude that is not already excluded by the access gate. Without it both
        # numbers come out 1 and the test passes whether scoping works or not.
        await _article(pg, mine, story_id=head.id, title="Mine as well", minutes_ago=5)

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg, in_scope={head.id, second.id})

        assert row.story_others == 1   # what unfolding would give back
        assert row.story_total == 2    # what the reader could open in the footer

    async def test_a_lone_member_offers_nothing_but_still_says_how_big(self, pg):
        """The case that decided the design: a feed labels everything, its
        counterpart feed is not labelled at all."""
        user = await _user(pg)
        head, _, _, _ = await _story(pg, user)

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg, in_scope={head.id})

        assert row.story_others == 0   # nothing to unfold: the line goes quiet
        assert row.story_total == 1    # but the story is still bigger than this row

    async def test_no_scope_means_the_whole_group(self, pg):
        """The unfiltered list, which must behave exactly as it did before."""
        user = await _user(pg)
        head, _, _, _ = await _story(pg, user)

        row = _item(head.id, story_id=head.story_id)
        await annotate([row], user.id, pg)

        assert row.story_others == row.story_total == 1


class TestListingOneStory:
    """``list_articles(story_id=...)`` feeds the rows the list unfolds under a group.

    A story is global, so this is one of the two list queries with no user-owned anchor
    of its own (search is the other) and it lives or dies on the access predicate.
    """

    async def test_the_whole_group_comes_back_whatever_state_it_is_in(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        pg.add(UserArticleState(user_id=user.id, article_id=second.id, is_read=True))
        await pg.flush()

        rows = await list_articles(user=user, db=pg, story_id=head.story_id)

        assert {r.id for r in rows} == {head.id, second.id}
        assert any(r.is_read for r in rows)

    async def test_a_feed_the_reader_does_not_take_stays_out(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        rows = await list_articles(user=user, db=pg, story_id=head.story_id)

        assert "Not subscribed" not in {r.title for r in rows}

    async def test_a_starred_member_survives_the_missing_subscription(self, pg):
        """The same rule the footer follows: a star keeps access to the article."""
        user = await _user(pg)
        head, _, _, theirs = await _story(pg, user)
        kept = await _article(pg, theirs, story_id=head.story_id, title="Starred")
        pg.add(UserArticleState(user_id=user.id, article_id=kept.id, is_starred=True))
        await pg.flush()

        rows = await list_articles(user=user, db=pg, story_id=head.story_id)

        assert kept.id in {r.id for r in rows}

    async def test_trimmed_members_stay_out(self, pg):
        user = await _user(pg)
        head, _, mine, _ = await _story(pg, user)
        gone = await _article(pg, mine, story_id=head.story_id, title="Old", trimmed=True)

        rows = await list_articles(user=user, db=pg, story_id=head.story_id)

        assert gone.id not in {r.id for r in rows}

    async def test_a_stranger_gets_nothing(self, pg):
        user = await _user(pg)
        stranger = await _user(pg)
        head, _, _, _ = await _story(pg, user)

        assert await list_articles(user=stranger, db=pg, story_id=head.story_id) == []


class TestMarkGroupRead:
    """Reading one article of a story settles the story.

    The row in the list stands for the event, so once the reader is done with it the
    rest must not come back as unread somewhere the list does not fold them — a label
    view, a feed, the next fetch. What must not happen is the other direction: an
    article the reader read themselves keeps its own read, because that is what the
    suppression rule (phase 4) triggers on and what retention counts as engagement.
    """

    async def _state(self, pg, user, article):
        return await pg.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id == article.id,
            )
        )

    async def test_the_rest_of_the_group_is_marked_read(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        closed = await mark_group_read(user.id, [head.id], pg)

        assert closed == [second.id]
        state = await self._state(pg, user, second)
        assert state.is_read is True

    async def test_they_are_marked_as_closed_on_the_reader_s_behalf(self, pg):
        """suppressed_at is what stops this from cascading: the machine closed these,
        so they can never themselves be the "you have seen this" a later article is
        suppressed against."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        await mark_group_read(user.id, [head.id], pg)

        assert (await self._state(pg, user, second)).suppressed_at is not None

    async def test_an_article_read_properly_keeps_its_own_read(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        read_at = NOW - timedelta(hours=2)
        pg.add(UserArticleState(user_id=user.id, article_id=second.id,
                                is_read=True, read_at=read_at))
        await pg.flush()

        closed = await mark_group_read(user.id, [head.id], pg)

        assert closed == []
        state = await self._state(pg, user, second)
        assert state.suppressed_at is None
        assert state.read_at == read_at

    async def test_a_feed_the_reader_does_not_take_is_not_touched(self, pg):
        user = await _user(pg)
        head, _, _, theirs = await _story(pg, user)
        outsider = (await pg.execute(
            select(Article).where(Article.feed_id == theirs.id)
        )).scalars().first()

        await mark_group_read(user.id, [head.id], pg)

        assert await self._state(pg, user, outsider) is None

    async def test_a_trimmed_member_is_not_touched(self, pg):
        user = await _user(pg)
        head, _, mine, _ = await _story(pg, user)
        stub = await _article(pg, mine, story_id=head.story_id, title="Old", trimmed=True)

        closed = await mark_group_read(user.id, [head.id], pg)

        assert stub.id not in closed

    async def test_an_article_with_no_story_closes_nothing(self, pg):
        user = await _user(pg)
        mine = await _feed(pg, user)
        alone = await _article(pg, mine, title="Alone")
        other = await _article(pg, mine, title="Unrelated")

        assert await mark_group_read(user.id, [alone.id], pg) == []
        assert await self._state(pg, user, other) is None

    async def test_nobody_else_s_state_is_written(self, pg):
        user = await _user(pg)
        stranger = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        await mark_group_read(user.id, [head.id], pg)

        assert await self._state(pg, stranger, second) is None

    async def test_the_scroll_batch_closes_the_group_too(self, pg):
        """The path that marks most articles read in practice, so the wiring matters
        as much as the rule."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        await mark_articles_read_batch(user, [head.id], pg)

        assert (await self._state(pg, user, second)).is_read is True

    async def test_an_unfolded_story_is_left_alone(self, pg):
        """The members are rows of their own on screen then, and they are read one by
        one like any other row. Closing what the reader just asked to see would be the
        opposite of what unfolding meant."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        await mark_articles_read_batch(user, [head.id], pg, unfolded_ids=[head.id])

        assert await self._state(pg, user, second) is None

    async def test_the_read_button_closes_a_folded_story(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        await toggle_article_state(user, head.id, "is_read", pg)

        assert (await self._state(pg, user, second)).is_read is True

    async def test_the_read_button_leaves_an_unfolded_one(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)

        await toggle_article_state(user, head.id, "is_read", pg, close_story=False)

        assert await self._state(pg, user, second) is None


class TestReopenGroup:
    """Taking the read back takes the story back.

    Closing a story is the one part of reading a row that reaches articles the reader
    never opened, so the undo has to reach them too. What it must not do is undo a
    read somebody actually made, or undo anything at all when the article being
    un-read was not what closed the group in the first place.
    """

    async def _state(self, pg, user, article):
        return await pg.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id == article.id,
            )
        )

    async def test_the_members_come_back_unread(self, pg):
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        await toggle_article_state(user, head.id, "is_read", pg)
        assert (await self._state(pg, user, second)).is_read is True

        await toggle_article_state(user, head.id, "is_read", pg)

        state = await self._state(pg, user, second)
        assert state.is_read is False
        assert state.read_at is None
        assert state.suppressed_at is None
        assert state.suppressed_by is None

    async def test_the_api_takes_it_back_the_same_way(self, pg):
        from app.schemas.article import ArticleStateUpdate
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        await update_article_state(user, head.id, ArticleStateUpdate(is_read=True), pg)

        await update_article_state(user, head.id, ArticleStateUpdate(is_read=False), pg)

        assert (await self._state(pg, user, second)).is_read is False

    async def test_a_member_read_properly_keeps_its_read(self, pg):
        """It was never closed on anyone's behalf, so there is nothing here to undo."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        read_at = NOW - timedelta(hours=2)
        pg.add(UserArticleState(user_id=user.id, article_id=second.id,
                                is_read=True, read_at=read_at))
        await pg.flush()
        await toggle_article_state(user, head.id, "is_read", pg)

        await toggle_article_state(user, head.id, "is_read", pg)

        state = await self._state(pg, user, second)
        assert state.is_read is True
        assert state.read_at == read_at

    async def test_un_reading_a_closed_member_reopens_nothing(self, pg):
        """The reader is saying they have not read this one. That says nothing about
        its siblings, and this member was not what closed them."""
        user = await _user(pg)
        head, second, mine, _ = await _story(pg, user)
        third = await _article(pg, mine, story_id=head.story_id, title="Third")
        await toggle_article_state(user, head.id, "is_read", pg)

        await toggle_article_state(user, second.id, "is_read", pg)

        assert (await self._state(pg, user, second)).is_read is False
        assert (await self._state(pg, user, third)).is_read is True

    async def test_an_unfolded_group_is_left_alone(self, pg):
        """Same flag as closing, and for the same reason: unfolded, the members are
        rows of their own and each answers for itself."""
        user = await _user(pg)
        head, second, _, _ = await _story(pg, user)
        await toggle_article_state(user, head.id, "is_read", pg)

        await toggle_article_state(user, head.id, "is_read", pg, close_story=False)

        assert (await self._state(pg, user, second)).is_read is True

    async def test_a_feed_the_reader_does_not_take_is_not_touched(self, pg):
        user = await _user(pg)
        stranger = await _user(pg)
        head, _, _, theirs = await _story(pg, user)
        outsider = (await pg.execute(
            select(Article).where(Article.feed_id == theirs.id)
        )).scalars().first()
        pg.add(UserArticleState(user_id=stranger.id, article_id=outsider.id,
                                is_read=True, suppressed_by="story",
                                suppressed_at=NOW))
        await pg.flush()
        await toggle_article_state(user, head.id, "is_read", pg)

        await toggle_article_state(user, head.id, "is_read", pg)

        assert (await self._state(pg, stranger, outsider)).is_read is True


class TestRowCount:
    """What the badges count. A badge stands above a list, so it counts what that list
    draws: one row per story, one per article without one."""

    async def _count(self, pg, user, *extra):
        from app.services.article import add_article_access_joins, article_access_predicate
        return await pg.scalar(
            add_article_access_joins(select(row_count()), user.id).where(
                Article.trimmed_at.is_(None), article_access_predicate(), *extra
            )
        )

    async def test_a_story_counts_once_however_many_sources(self, pg):
        user = await _user(pg)
        head, _, mine, _ = await _story(pg, user)
        # Two members in feeds this reader takes, one in a feed they do not.
        assert await self._count(pg, user, Article.story_id == head.story_id) == 1

    async def test_articles_without_a_story_count_one_each(self, pg):
        user = await _user(pg)
        mine = await _feed(pg, user)
        a = await _article(pg, mine, title="One")
        b = await _article(pg, mine, title="Two")

        assert await self._count(pg, user, Article.id.in_([a.id, b.id])) == 2

    async def test_a_story_and_a_loose_article_do_not_collide(self, pg):
        """coalesce(story_id, -id) keys the two halves apart; ids being positive is
        what makes that safe."""
        user = await _user(pg)
        head, second, mine, _ = await _story(pg, user)
        loose = await _article(pg, mine, title="Loose")

        assert await self._count(
            pg, user, Article.id.in_([head.id, second.id, loose.id])
        ) == 2

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
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.schemas.article import ArticleListItem
from app.services.article import list_articles
from app.services.story_service import (
    MAX_SHOWN_STORIES,
    annotate,
    collapse_page,
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

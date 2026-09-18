"""Integration tests for cross-source story grouping (app.fetcher.stories).

Runs against the real (dev) database inside a transaction that is always rolled back.
Real Postgres is not optional here: the whole mechanism is a generated column, a pg_trgm
index and a similarity threshold, none of which a mocked session can tell us anything
about. Skips automatically if the database is unreachable.

Every title carries a per-test nonce word so the fixtures can never pair up with real
articles that happen to sit in the development database.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher.rss import _dedup_cross_feed
from app.fetcher.stories import assign_stories, assign_stories_global
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User

NOW = datetime.now(timezone.utc)

TRAM = "city council approves the new tram line"
TRAM_REWORDED = "new tram line approved by the city council"
TRAM_THIRD = "council approved a new tram line, says the city"
RATES = "central bank raises interest rates again"


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


@pytest_asyncio.fixture
def nonce():
    """Short shared token that isolates a test's titles from real articles.

    Short on purpose: it lands in every title of the test, so a long one would lift the
    similarity of titles the test needs to stay far apart.
    """
    return "zz" + uuid.uuid4().hex[:4]


async def _user(session) -> User:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"story_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    return user


async def _feed(session, user=None) -> Feed:
    u = uuid.uuid4().hex[:12]
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    if user is not None:
        session.add(UserFeed(user_id=user.id, feed_id=feed.id))
        await session.flush()
    return feed


async def _article(session, feed, title, *, hours_ago=0, url=None) -> Article:
    u = uuid.uuid4().hex
    article = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=title,
        url=url, url_normalized=url,
        published_at=NOW - timedelta(hours=hours_ago),
        fetched_at=NOW - timedelta(hours=hours_ago),
    )
    session.add(article)
    await session.flush()
    return article


async def _story_of(session, article) -> int | None:
    return (await session.execute(
        select(Article.story_id).where(Article.id == article.id)
    )).scalar_one()


# ── title_norm (generated column) ─────────────────────────────────────────────

class TestTitleNorm:
    async def test_lowercased_and_unaccented(self, pg):
        feed = await _feed(pg)
        article = await _article(pg, feed, "Přílíš Žluťoučký KŮŇ")
        await pg.refresh(article)
        assert article.title_norm == "prilis zlutoucky kun"

    async def test_follows_a_renamed_title(self, pg):
        """It is generated, not copied — nothing in the app has to remember to update it."""
        feed = await _feed(pg)
        article = await _article(pg, feed, "First title of the article")
        article.title = "Second title of the article"
        await pg.flush()
        await pg.refresh(article)
        assert article.title_norm == "second title of the article"


# ── grouping ──────────────────────────────────────────────────────────────────

class TestAssignStories:
    async def test_two_feeds_same_story_share_the_oldest_id(self, pg, nonce):
        older = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=2)
        newer = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}")

        assert await assign_stories([older, newer], pg) == 2

        assert await _story_of(pg, older) == older.id
        assert await _story_of(pg, newer) == older.id

    async def test_same_feed_is_not_a_story(self, pg, nonce):
        """A newsroom rewriting its own headline is not cross-source coverage."""
        feed = await _feed(pg)
        first = await _article(pg, feed, f"{nonce} {TRAM}", hours_ago=2)
        second = await _article(pg, feed, f"{nonce} {TRAM_REWORDED}")

        await assign_stories([first, second], pg)

        assert await _story_of(pg, first) is None
        assert await _story_of(pg, second) is None

    async def test_unrelated_titles_stay_apart(self, pg, nonce):
        tram = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=2)
        rates = await _article(pg, await _feed(pg), f"{nonce} {RATES}")

        await assign_stories([tram, rates], pg)

        assert await _story_of(pg, tram) is None
        assert await _story_of(pg, rates) is None

    async def test_window_holds(self, pg, nonce):
        """Coverage four days apart is a new story about the same subject."""
        old = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=96)
        fresh = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}")

        await assign_stories([old, fresh], pg)

        assert await _story_of(pg, fresh) is None

    async def test_inside_window_still_groups(self, pg, nonce):
        old = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=71)
        fresh = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}")

        await assign_stories([old, fresh], pg)

        assert await _story_of(pg, fresh) == old.id

    async def test_third_source_inherits_the_group(self, pg, nonce):
        first = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=3)
        second = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}", hours_ago=2)
        await assign_stories([first, second], pg)

        third = await _article(pg, await _feed(pg), f"{nonce} {TRAM_THIRD}")
        assert await assign_stories([third], pg) == 1

        assert await _story_of(pg, third) == first.id
        assert await _story_of(pg, second) == first.id

    async def test_merging_two_groups_moves_every_member(self, pg, nonce):
        """The race the post-gather pass exists for: two groups meet through one article.

        The bystander is the point — it never matches the bridging article itself, so it
        can only move if the merge follows story_id rather than the matched pairs.
        """
        left = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=3)
        right = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}", hours_ago=2)
        bystander = await _article(pg, await _feed(pg), f"{nonce} {RATES}", hours_ago=2)
        left.story_id = left.id
        right.story_id = right.id
        bystander.story_id = right.id
        await pg.flush()

        bridge = await _article(pg, await _feed(pg), f"{nonce} {TRAM_THIRD}")
        await assign_stories([bridge], pg)

        assert await _story_of(pg, bridge) == left.id
        assert await _story_of(pg, right) == left.id
        assert await _story_of(pg, bystander) == left.id

    async def test_short_titles_are_never_grouped(self, pg, nonce):
        """Two title-less items are not the same story, they are two missing titles."""
        first = await _article(pg, await _feed(pg), "Untitled", hours_ago=2)
        second = await _article(pg, await _feed(pg), "Untitled")

        await assign_stories([first, second], pg)

        assert await _story_of(pg, first) is None
        assert await _story_of(pg, second) is None

    async def test_empty_batch_touches_nothing(self, pg):
        assert await assign_stories([], pg) == 0

    async def test_lone_article_gets_no_story(self, pg, nonce):
        lone = await _article(pg, await _feed(pg), f"{nonce} {TRAM}")

        assert await assign_stories([lone], pg) == 0
        assert await _story_of(pg, lone) is None


class TestAssignStoriesGlobal:
    async def test_only_this_round_is_scanned_but_older_articles_still_match(self, pg, nonce):
        """The post-gather pass works on what the round brought in; everything already
        in the window is fair game as a counterpart."""
        earlier = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=10)
        arrived = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}")

        # The count is not asserted: this pass is global, so in a development database
        # it also groups whatever the real feeds brought in during the same hour.
        assert await assign_stories_global(NOW - timedelta(hours=1), pg) >= 1
        assert await _story_of(pg, arrived) == earlier.id
        assert await _story_of(pg, earlier) == earlier.id

    async def test_nothing_fetched_since_means_nothing_to_do(self, pg, nonce):
        earlier = await _article(pg, await _feed(pg), f"{nonce} {TRAM}", hours_ago=10)
        arrived = await _article(pg, await _feed(pg), f"{nonce} {TRAM_REWORDED}")

        assert await assign_stories_global(NOW + timedelta(hours=1), pg) == 0

        assert await _story_of(pg, earlier) is None
        assert await _story_of(pg, arrived) is None


# ── machine reads are marked ──────────────────────────────────────────────────

class TestSuppressedAt:
    async def test_url_dedup_marks_the_read_as_machine_written(self, pg):
        """Without suppressed_at this read would later pass for "the user saw it"."""
        user = await _user(pg)
        feed_a = await _feed(pg, user)
        feed_b = await _feed(pg, user)
        url = f"https://ex.invalid/{uuid.uuid4().hex}"
        await _article(pg, feed_a, "Syndicated article title", url=url)
        duplicate = await _article(pg, feed_b, "Syndicated article title", url=url)

        await _dedup_cross_feed(feed_b.id, [duplicate], pg)

        state = (await pg.execute(
            select(UserArticleState).where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id == duplicate.id,
            )
        )).scalar_one()
        assert state.is_read is True
        assert state.suppressed_at is not None

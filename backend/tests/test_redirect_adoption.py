"""Integration tests: adopting a feed's address after a permanent redirect.

The unit tests in test_url_validator.py cover *which* address a chain yields. These
cover what gets written to the feeds row, against real Postgres, because the
interesting parts are the partial unique indexes from migration 0037 (two feeds
converging on one URL) and the fact that adoption commits in its own transaction
after the fetch has already committed.
"""
import uuid
from unittest.mock import AsyncMock, patch

import feedparser
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher.redirects import (
    _redirect_conflicts,
    adopt_permanent_url,
    redirect_conflicts,
)
from app.fetcher.rss import fetch_feed
from app.fetcher.scrape import fetch_scrape_feed
from app.models.feed import Feed
from app.models.user import User
from app.services.feed import subscribe
from app.utils.url_validator import ConditionalResponse, PageResponse

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pg():
    """A real session with real commits (adoption commits; see test_failure_db)."""
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
        await conn.close()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    session = AsyncSession(bind=engine, expire_on_commit=False)
    created: list[int] = []
    users: list[int] = []
    session.info["created_feed_ids"] = created
    session.info["created_user_ids"] = users
    try:
        yield session
    finally:
        await session.rollback()
        if users:
            await session.execute(delete(User).where(User.id.in_(users)))
        if created:
            await session.execute(delete(Feed).where(Feed.id.in_(created)))
        await session.commit()
        await session.close()
        await engine.dispose()


def _url(suffix: str = "") -> str:
    return f"https://example.com/{uuid.uuid4().hex}{suffix}.xml"


async def _feed(session, **kwargs) -> Feed:
    u = uuid.uuid4().hex
    kwargs.setdefault("feed_url", f"https://example.com/{u}.xml")
    kwargs.setdefault("title", f"f-{u[:6]}")
    feed = Feed(subscriber_count=1, **kwargs)
    session.add(feed)
    await session.commit()
    session.info["created_feed_ids"].append(feed.id)
    return feed


_EMPTY_PARSE = feedparser.FeedParserDict(
    {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
)


async def _fetch(session, feed, *, status=200, permanent_url=None, parse=_EMPTY_PARSE):
    """Run one RSS fetch whose response reports *permanent_url*."""
    resp = ConditionalResponse(status, "<rss/>", None, None, permanent_url=permanent_url)
    with (
        patch("app.fetcher.rss.async_validate_feed_url", new_callable=AsyncMock),
        patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
        patch("app.fetcher.rss.feedparser.parse", return_value=parse),
    ):
        await fetch_feed(feed, session)
    await session.refresh(feed)
    return feed


class TestRssAdoption:
    async def test_200_stores_the_new_address(self, pg):
        feed = await _feed(pg)
        moved = _url("-moved")
        await _fetch(pg, feed, permanent_url=moved)
        assert feed.feed_url == moved

    async def test_304_stores_the_new_address(self, pg):
        # A conditional request that redirects still learns where the feed lives.
        feed = await _feed(pg, etag='"abc"')
        moved = _url("-moved")
        await _fetch(pg, feed, status=304, permanent_url=moved)
        assert feed.feed_url == moved

    async def test_no_permanent_redirect_leaves_the_url_alone(self, pg):
        feed = await _feed(pg)
        original = feed.feed_url
        await _fetch(pg, feed, permanent_url=None)
        assert feed.feed_url == original

    async def test_failed_fetch_never_adopts(self, pg):
        # The usual way a feed dies is a 301 to the site homepage. The parse has to
        # fail first, or the feed would permanently point at that homepage.
        feed = await _feed(pg)
        original = feed.feed_url
        bozo = feedparser.FeedParserDict(
            {"bozo": True, "bozo_exception": ValueError("not a feed"), "entries": []}
        )
        await _fetch(pg, feed, permanent_url=_url("-homepage"), parse=bozo)
        assert feed.feed_url == original
        assert feed.fetch_error_count == 1

    async def test_url_already_taken_by_another_feed_is_skipped(self, pg):
        # Two feeds converging on one address: the loser keeps walking its redirect
        # rather than tripping the partial unique index.
        target = _url("-shared")
        await _feed(pg, feed_url=target)
        feed = await _feed(pg)
        original = feed.feed_url
        await _fetch(pg, feed, permanent_url=target)
        assert feed.feed_url == original

    async def test_url_held_by_a_scrape_feed_is_not_a_conflict(self, pg):
        # An RSS feed and a scrape feed at one address are two different things
        # (parse it as XML vs. scrape it as HTML), and the unique index that keeps
        # public RSS feeds unique excludes scrape rows. Nothing to collide with.
        target = _url("-shared")
        await _feed(pg, feed_url=target, feed_type="scrape",
                    type_config={"article_links_selector": "article a"})
        feed = await _feed(pg)
        await _fetch(pg, feed, permanent_url=target)
        assert feed.feed_url == target

    async def test_private_feed_may_take_a_public_feeds_url(self, pg):
        # The unique index covers public non-scrape feeds only, so a private feed
        # converging on a public URL is not a conflict.
        target = _url("-public")
        await _feed(pg, feed_url=target)
        feed = await _feed(pg, is_private=True)
        adopted = await adopt_permanent_url(
            feed.id, feed.feed_url, target, pg, is_private=True
        )
        assert adopted is True


class TestSubscribeResolvesBeforeCreating:
    """Feeds are created on the address the host serves, not the one that was typed."""

    async def _user(self, session) -> User:
        u = uuid.uuid4().hex
        user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}")
        session.add(user)
        await session.commit()
        session.info["created_user_ids"].append(user.id)
        return user

    async def _subscribe(self, session, user, url, *, permanent_url=None):
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({"title": "T"})}
        )
        with (
            patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock),
            patch("app.services.feed.fetch_and_parse_url",
                  new=AsyncMock(return_value=(parsed, permanent_url))),
        ):
            uf = await subscribe(
                user=user, url=url, folder_id=None, custom_title=None,
                fetch_auth_user=None, fetch_auth_pass=None, db=session,
                trigger_initial_fetch=False,
            )
        session.info["created_feed_ids"].append(uf.feed_id)
        return uf

    async def test_new_feed_is_created_on_the_real_address(self, pg):
        user = await self._user(pg)
        typed, real = _url("-typed"), _url("-real")
        uf = await self._subscribe(pg, user, typed, permanent_url=real)
        feed = await pg.get(Feed, uf.feed_id)
        assert feed.feed_url == real

    async def test_stale_url_joins_the_existing_feed_instead_of_duplicating(self, pg):
        # The OPML re-import case: the export carries the old URL, but the feed at
        # the new one is already subscribed. Without resolving first, this would
        # create a second row that could never adopt the taken address.
        real = _url("-real")
        existing = await _feed(pg, feed_url=real)
        user = await self._user(pg)
        uf = await self._subscribe(pg, user, _url("-stale"), permanent_url=real)
        assert uf.feed_id == existing.id

    async def test_no_redirect_keeps_the_requested_url(self, pg):
        user = await self._user(pg)
        typed = _url("-typed")
        uf = await self._subscribe(pg, user, typed, permanent_url=None)
        feed = await pg.get(Feed, uf.feed_id)
        assert feed.feed_url == typed


class TestScrapeAdoption:
    _SELECTOR = "article h2 a"
    _HTML = '<html><body><article><h2><a href="/a">A</a></h2></article></body></html>'

    async def _scrape_feed(self, session, **kwargs):
        kwargs.setdefault("type_config", {"article_links_selector": self._SELECTOR})
        return await _feed(session, feed_type="scrape", **kwargs)

    async def _scrape(self, session, feed, *, html=None, permanent_url=None):
        page = PageResponse(self._HTML if html is None else html, permanent_url)
        with (
            patch("app.fetcher.scrape.async_validate_feed_url", new_callable=AsyncMock),
            patch("app.fetcher.scrape.fetch_url_page", return_value=page),
            patch("app.fetcher.scrape._save_scrape_articles", return_value=0),
        ):
            await fetch_scrape_feed(feed, session)
        await session.refresh(feed)
        return feed

    async def test_stores_the_new_address(self, pg):
        feed = await self._scrape_feed(pg)
        moved = _url("-moved")
        await self._scrape(pg, feed, permanent_url=moved)
        assert feed.feed_url == moved

    async def test_selector_matching_nothing_never_adopts(self, pg):
        # Redirected to a page this feed cannot scrape: keep the old address.
        feed = await self._scrape_feed(pg)
        original = feed.feed_url
        await self._scrape(pg, feed, html="<html><body></body></html>",
                           permanent_url=_url("-elsewhere"))
        assert feed.feed_url == original

    async def test_same_url_with_a_different_selector_is_not_a_conflict(self, pg):
        # Scrape feeds are unique on (feed_url, selector), so another selector on
        # the target URL must not block the rewrite.
        target = _url("-shared")
        await self._scrape_feed(pg, feed_url=target,
                                type_config={"article_links_selector": ".other a"})
        feed = await self._scrape_feed(pg)
        await self._scrape(pg, feed, permanent_url=target)
        assert feed.feed_url == target

    async def test_url_held_by_an_rss_feed_is_not_a_conflict(self, pg):
        # Mirror of the RSS case: scrape feeds are unique on (url, selector), an
        # index no RSS row takes part in.
        target = _url("-shared")
        await _feed(pg, feed_url=target)
        feed = await self._scrape_feed(pg)
        await self._scrape(pg, feed, permanent_url=target)
        assert feed.feed_url == target

    async def test_same_url_and_selector_is_a_conflict(self, pg):
        target = _url("-shared")
        await self._scrape_feed(pg, feed_url=target)
        feed = await self._scrape_feed(pg)
        original = feed.feed_url
        await self._scrape(pg, feed, permanent_url=target)
        assert feed.feed_url == original


class TestConflictRegistry:
    """A blocked adoption is recorded so the admin dashboard can surface the pair."""

    def setup_method(self):
        _redirect_conflicts.clear()

    teardown_method = setup_method

    async def test_conflict_is_recorded_then_cleared_on_resolution(self, pg):
        target = _url("-shared")
        holder = await _feed(pg, feed_url=target)
        feed = await _feed(pg)

        # Redirected onto a held URL: the pair is registered for the admin view.
        await _fetch(pg, feed, permanent_url=target)
        conflicts = redirect_conflicts()
        assert set(conflicts) == {feed.id}
        assert conflicts[feed.id].target_url == target
        assert conflicts[feed.id].holder_id == holder.id

        # A later fetch that redirects somewhere free adopts and clears the record.
        free = _url("-free")
        await _fetch(pg, feed, permanent_url=free)
        assert feed.feed_url == free
        assert redirect_conflicts() == {}

    async def test_successful_adoption_leaves_no_record(self, pg):
        feed = await _feed(pg)
        await _fetch(pg, feed, permanent_url=_url("-moved"))
        assert redirect_conflicts() == {}

    async def test_admin_service_joins_titles(self, pg):
        from app.services.admin_service import list_redirect_conflicts

        target = _url("-shared")
        holder = await _feed(pg, feed_url=target, title="Holder feed")
        feed = await _feed(pg, title="Moving feed")
        await _fetch(pg, feed, permanent_url=target)

        rows = await list_redirect_conflicts(pg)
        assert len(rows) == 1
        row = rows[0]
        assert row["feed_id"] == feed.id and row["feed_title"] == "Moving feed"
        assert row["holder_id"] == holder.id and row["holder_title"] == "Holder feed"
        assert row["target_url"] == target

    async def test_admin_service_empty_without_conflicts(self, pg):
        from app.services.admin_service import list_redirect_conflicts
        assert await list_redirect_conflicts(pg) == []

    async def test_admin_service_redacts_a_token_in_the_target_url(self, pg):
        from app.services.admin_service import list_redirect_conflicts

        target = f"https://example.com/{uuid.uuid4().hex}.xml?api_key=SECRET"
        await _feed(pg, feed_url=target)
        feed = await _feed(pg)
        await _fetch(pg, feed, permanent_url=target)

        row = (await list_redirect_conflicts(pg))[0]
        assert "SECRET" not in row["target_url"]
        assert row["target_url"].endswith("?<redacted>")

    async def test_db_error_during_adoption_is_swallowed(self, pg):
        """A DB failure while adopting must not propagate: the caller already
        committed the fetch, so a raise here would record a false fetch error."""
        feed = await _feed(pg)
        original = feed.feed_url
        with patch.object(pg, "execute", new=AsyncMock(side_effect=RuntimeError("boom"))):
            # is_private=True skips the conflict SELECT and goes straight to the write.
            result = await adopt_permanent_url(
                feed.id, original, _url("-moved"), pg, is_private=True
            )
        assert result is False
        # Session recovered (rolled back) and the URL is untouched.
        await pg.refresh(feed)
        assert feed.feed_url == original

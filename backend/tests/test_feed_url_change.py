"""Integration tests: pointing an existing feed at a different address.

Against real Postgres, because what makes the operation worth testing is the state it
leaves on the feeds row (articles kept, validators and failure trail dropped) and the
partial unique indexes from migration 0037 that decide whether the new address is free.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import feedparser
import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed
from app.services.feed import FeedUrlTaken, change_feed_url, may_edit_feed_url
from app.utils.crypto import decrypt

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pg():
    """A real session with real commits (change_feed_url commits its own write)."""
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
    session.info["created_feed_ids"] = created
    try:
        yield session
    finally:
        await session.rollback()
        if created:
            await session.execute(delete(Article).where(Article.feed_id.in_(created)))
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
    kwargs.setdefault("subscriber_count", 1)
    feed = Feed(**kwargs)
    session.add(feed)
    await session.commit()
    session.info["created_feed_ids"].append(feed.id)
    return feed


_PARSED = feedparser.FeedParserDict(
    {"bozo": False, "entries": [{"title": "x"}], "feed": feedparser.FeedParserDict({})}
)


def _verified(permanent_url: str | None = None):
    """Patch the confirming fetch to succeed (optionally reporting a redirect)."""
    from app.fetcher.rss import ParsedFeed
    return (
        patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock),
        patch(
            "app.services.feed.fetch_and_parse_url",
            new_callable=AsyncMock,
            return_value=ParsedFeed(_PARSED, permanent_url),
        ),
    )


async def _change(session, feed, new_url, **kwargs) -> bool:
    validate, fetch = _verified(kwargs.pop("permanent_url", None))
    with validate, fetch:
        return await change_feed_url(session, feed, new_url, **kwargs)


class TestMayEditFeedUrl:
    """Who is allowed to ask, in the same shape as may_edit_feed_auth."""

    async def test_sole_subscriber_may(self):
        assert may_edit_feed_url(Feed(subscriber_count=1)) is True

    async def test_shared_feed_may_not(self):
        assert may_edit_feed_url(Feed(subscriber_count=4)) is False

    async def test_unsubscribed_feed_is_not_a_subscriber_s_to_edit(self):
        # Nobody's to change from the subscriber side; the admin panel is the way in.
        assert may_edit_feed_url(Feed(subscriber_count=0)) is False


class TestChangeFeedUrl:
    async def test_stores_the_new_address(self, pg):
        feed = await _feed(pg)
        moved = _url("-moved")
        assert await _change(pg, feed, moved) is True
        await pg.refresh(feed)
        assert feed.feed_url == moved

    async def test_articles_survive_the_move(self, pg):
        feed = await _feed(pg)
        pg.add(Article(
            feed_id=feed.id, title="kept", url=_url("-article"),
            guid=uuid.uuid4().hex, guid_hash=uuid.uuid4().hex,
            published_at=datetime.now(timezone.utc),
        ))
        await pg.commit()
        await _change(pg, feed, _url("-moved"))
        kept = await pg.scalar(
            select(Article.title).where(Article.feed_id == feed.id)
        )
        assert kept == "kept"

    async def test_same_address_is_not_a_change(self, pg):
        feed = await _feed(pg)
        assert await _change(pg, feed, feed.feed_url) is False

    async def test_redirect_target_is_what_gets_stored(self, pg):
        feed = await _feed(pg)
        target = _url("-target")
        await _change(pg, feed, _url("-asked"), permanent_url=target)
        await pg.refresh(feed)
        assert feed.feed_url == target

    async def test_validators_are_dropped(self, pg):
        # They describe the old address' body: kept, the first poll of the new address
        # would be a conditional request whose 304 means nothing.
        feed = await _feed(pg, etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT")
        await _change(pg, feed, _url("-moved"))
        await pg.refresh(feed)
        assert feed.etag is None
        assert feed.last_modified is None

    async def test_failure_trail_is_cleared(self, pg):
        feed = await _feed(
            pg, status="disabled", fetch_error_count=6, block_count=2,
            last_error="Feed parse error",
            retry_after_until=datetime.now(timezone.utc) + timedelta(hours=12),
            last_fetched_at=datetime.now(timezone.utc),
        )
        await _change(pg, feed, _url("-moved"))
        await pg.refresh(feed)
        assert feed.status == "active"
        assert feed.fetch_error_count == 0
        assert feed.block_count == 0
        assert feed.last_error is None
        assert feed.retry_after_until is None
        # Due at the next scheduler tick, not one interval after the old address' poll.
        assert feed.last_fetched_at is None

    async def test_address_held_by_another_public_feed_is_refused(self, pg):
        taken = await _feed(pg)
        feed = await _feed(pg)
        original = feed.feed_url
        with pytest.raises(FeedUrlTaken) as exc:
            await _change(pg, feed, taken.feed_url)
        assert exc.value.feed_id == taken.id
        await pg.refresh(feed)
        assert feed.feed_url == original

    async def test_a_private_feed_may_take_a_public_feed_s_address(self, pg):
        # The unique indexes cover public rows only (migration 0037), so this is legal:
        # one person's credentialed copy of a feed everybody else reads anonymously.
        public = await _feed(pg)
        feed = await _feed(pg, is_private=True, fetch_auth_user="u")
        assert await _change(pg, feed, public.feed_url) is True

    async def test_scrape_feed_keeps_its_selector_and_moves_its_site_url(self, pg):
        feed = await _feed(
            pg, feed_type="scrape", site_url="https://example.com/old",
            type_config={"article_links_selector": "h2 a"},
        )
        moved = "https://example.com/new-page"
        with (
            patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock),
            patch("app.fetcher.scrape.fetch_page_html", new_callable=AsyncMock, return_value="<html/>"),
            patch("app.fetcher.scrape.extract_article_links", return_value=["https://example.com/a"]),
        ):
            await change_feed_url(pg, feed, moved)
        await pg.refresh(feed)
        assert feed.feed_url == moved
        assert feed.site_url == moved
        assert feed.type_config["article_links_selector"] == "h2 a"

    async def test_scrape_feed_whose_selector_matches_nothing_is_refused(self, pg):
        feed = await _feed(
            pg, feed_type="scrape", type_config={"article_links_selector": "h2 a"},
        )
        original = feed.feed_url
        with (
            patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock),
            patch("app.fetcher.scrape.fetch_page_html", new_callable=AsyncMock, return_value="<html/>"),
            patch("app.fetcher.scrape.extract_article_links", return_value=[]),
        ):
            with pytest.raises(ValueError, match="matched no article links"):
                await change_feed_url(pg, feed, "https://example.com/new-page")
        await pg.refresh(feed)
        assert feed.feed_url == original


class TestChangeFeedUrlVerification:
    async def test_an_address_that_does_not_parse_is_refused(self, pg):
        feed = await _feed(pg)
        original = feed.feed_url
        with (
            patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock),
            patch("app.services.feed.fetch_and_parse_url", new_callable=AsyncMock,
                  side_effect=ValueError("The server returned a web page, not a feed.")),
        ):
            with pytest.raises(ValueError, match="not a feed"):
                await change_feed_url(pg, feed, _url("-broken"))
        await pg.refresh(feed)
        assert feed.feed_url == original

    async def test_a_blocked_address_never_reaches_the_row(self, pg):
        # SSRF validation runs before anything is written (and before the fetch).
        feed = await _feed(pg)
        original = feed.feed_url
        with patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock,
                   side_effect=ValueError("Cannot resolve hostname")):
            with pytest.raises(ValueError, match="Cannot resolve hostname"):
                await change_feed_url(pg, feed, "https://internal.invalid/feed.xml")
        await pg.refresh(feed)
        assert feed.feed_url == original

    async def test_verify_false_skips_the_fetch(self, pg):
        # The admin's way to fix an address while the host is down.
        feed = await _feed(pg)
        moved = _url("-moved")
        with (
            patch("app.services.feed.async_validate_feed_url", new_callable=AsyncMock),
            patch("app.services.feed.fetch_and_parse_url", new_callable=AsyncMock) as fetch,
        ):
            assert await change_feed_url(pg, feed, moved, verify=False) is True
        fetch.assert_not_called()
        await pg.refresh(feed)
        assert feed.feed_url == moved

    async def test_empty_address_is_refused(self, pg):
        feed = await _feed(pg)
        with pytest.raises(ValueError, match="cannot be empty"):
            await change_feed_url(pg, feed, "   ")


class TestChangeFeedUrlCredentials:
    """Credentials written into the address are moved out of it, as on subscribe."""

    async def test_credentials_move_into_the_auth_columns(self, pg):
        feed = await _feed(pg)
        await _change(pg, feed, "https://joe:s3cret@example.com/private.xml")
        await pg.refresh(feed)
        assert feed.feed_url == "https://example.com/private.xml"
        assert feed.fetch_auth_user == "joe"
        assert decrypt(feed.fetch_auth_pass_encrypted) == "s3cret"
        assert feed.is_private is True

    async def test_a_shared_feed_refuses_credentials(self, pg):
        # Writing them would make somebody else's subscription private, with one
        # person's password fetching for another.
        feed = await _feed(pg, subscriber_count=3)
        original = feed.feed_url
        with pytest.raises(ValueError, match="shared with other people"):
            await _change(pg, feed, "https://joe:s3cret@example.com/private.xml")
        await pg.refresh(feed)
        assert feed.feed_url == original
        assert feed.fetch_auth_user is None

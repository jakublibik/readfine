"""Integration tests: what a failed fetch actually writes to the feeds row.

The unit tests in test_failure.py cover the decision. These run it against real
Postgres, because the interesting parts are SQL expressions — `Feed.block_count + 1`
and a CASE over the pre-increment value — whose effect cannot be read off the clause
itself. An earlier version of these tests inspected the SQLAlchemy expression with a
helper that only recognised a bare literal, and so reported "not disabled" for every
CASE regardless of what it evaluated to.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher.failure import (
    BLOCK_BACKOFF_BASE,
    BLOCK_DISABLE_THRESHOLD,
    FETCH_ERROR_DISABLE_THRESHOLD,
    NOT_FOUND_DISABLE_THRESHOLD,
)
from app.fetcher.rss import fetch_feed
from app.models.feed import Feed

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def pg():
    """A real session with real commits.

    ``fetch_feed`` rolls back and then commits inside its own error handler, so the
    usual "wrap the test in a transaction and roll it back" fixture does not survive
    it. Rows are created for real and deleted afterwards instead; ``fetch_logs``
    cascade with the feed.
    """
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
        if created:
            await session.rollback()
            await session.execute(delete(Feed).where(Feed.id.in_(created)))
            await session.commit()
        await session.close()
        await engine.dispose()


async def _feed(session, **kwargs) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(
        # Resolvable on purpose: fetch_feed validates the URL (DNS included) before it
        # reaches the patched fetch, so an unresolvable host would fail earlier and
        # never exercise the tier we are testing.
        feed_url=f"https://example.com/{u}.xml",
        title=f"f-{u[:6]}",
        subscriber_count=1,
        **kwargs,
    )
    session.add(feed)
    await session.commit()
    session.info["created_feed_ids"].append(feed.id)
    return feed


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://ex.invalid/feed.xml")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(str(status), request=request, response=response)


async def _fail(session, feed, exc):
    """Run one failing fetch and refresh the feed to its committed row state."""
    with (
        patch("app.fetcher.rss.async_validate_feed_url", new_callable=AsyncMock),
        patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc),
    ):
        await fetch_feed(feed, session)
    await session.refresh(feed)
    return feed


class TestBlockTierWrites:
    async def test_403_increments_block_count_and_leaves_status(self, pg):
        feed = await _feed(pg, status="active", fetch_error_count=0, block_count=0)
        await _fail(pg, feed, _http_error(403))
        assert feed.block_count == 1
        assert feed.fetch_error_count == 0
        assert feed.status == "active"

    async def test_repeated_blocks_accumulate(self, pg):
        feed = await _feed(pg, status="active", block_count=0)
        for expected in (1, 2, 3):
            await _fail(pg, feed, _http_error(403))
            assert feed.block_count == expected
        assert feed.status == "active"

    async def test_block_below_threshold_does_not_disable(self, pg):
        feed = await _feed(pg, status="active", block_count=BLOCK_DISABLE_THRESHOLD - 2)
        await _fail(pg, feed, _http_error(403))
        assert feed.status == "active"

    async def test_block_at_threshold_disables(self, pg):
        # The CASE compares the pre-increment value, so the feed goes down on the
        # block after the threshold is reached.
        feed = await _feed(pg, status="active", block_count=BLOCK_DISABLE_THRESHOLD)
        await _fail(pg, feed, _http_error(403))
        assert feed.status == "disabled"
        assert feed.block_count == BLOCK_DISABLE_THRESHOLD + 1

    async def test_block_defers_the_feed(self, pg):
        feed = await _feed(pg, status="active", block_count=0)
        before = datetime.now(timezone.utc)
        await _fail(pg, feed, _http_error(403))
        assert abs((feed.retry_after_until - (before + BLOCK_BACKOFF_BASE)).total_seconds()) < 5

    async def test_block_preserves_an_existing_error_state(self, pg):
        feed = await _feed(pg, status="error", fetch_error_count=3, block_count=0)
        await _fail(pg, feed, _http_error(403))
        assert feed.status == "error"
        assert feed.fetch_error_count == 3
        assert feed.block_count == 1


class TestErrorTierWrites:
    async def test_timeout_increments_error_count(self, pg):
        feed = await _feed(pg, status="active", fetch_error_count=0, block_count=0)
        await _fail(pg, feed, httpx.ConnectTimeout("timed out"))
        assert feed.fetch_error_count == 1
        assert feed.block_count == 0
        assert feed.status == "error"

    async def test_error_at_threshold_disables(self, pg):
        feed = await _feed(pg, status="error", fetch_error_count=FETCH_ERROR_DISABLE_THRESHOLD)
        await _fail(pg, feed, httpx.ConnectTimeout("timed out"))
        assert feed.status == "disabled"

    @pytest.mark.parametrize("status", [410, 451, 400])
    async def test_permanent_4xx_disables_immediately(self, pg, status):
        feed = await _feed(pg, status="active", fetch_error_count=0)
        await _fail(pg, feed, _http_error(status))
        assert feed.status == "disabled"

    async def test_403_with_credentials_prompt_uses_the_error_tier(self, pg):
        feed = await _feed(pg, status="active", fetch_error_count=0, block_count=0)
        await _fail(pg, feed, _http_error(403, {"WWW-Authenticate": 'Basic realm="feeds"'}))
        assert feed.fetch_error_count == 1
        assert feed.block_count == 0
        assert feed.status == "error"


class TestNotFoundTierWrites:
    """A 404 is the one 4xx that gets retried: hosts serve it for their own
    transient failures, so it goes through the counter on a shorter threshold."""

    async def test_404_does_not_disable_on_the_first_hit(self, pg):
        feed = await _feed(pg, status="active", fetch_error_count=0)
        await _fail(pg, feed, _http_error(404))
        assert feed.status == "error"
        assert feed.fetch_error_count == 1

    async def test_404_survives_a_run_below_the_threshold(self, pg):
        feed = await _feed(pg, status="active", fetch_error_count=0)
        for _ in range(NOT_FOUND_DISABLE_THRESHOLD):
            await _fail(pg, feed, _http_error(404))
        assert feed.status == "error"
        assert feed.fetch_error_count == NOT_FOUND_DISABLE_THRESHOLD

    async def test_404_disables_at_the_threshold(self, pg):
        # Same pre-increment comparison as the other tiers: the feed goes down on
        # the failure after the threshold is reached.
        feed = await _feed(pg, status="error", fetch_error_count=NOT_FOUND_DISABLE_THRESHOLD)
        await _fail(pg, feed, _http_error(404))
        assert feed.status == "disabled"

    async def test_404_disables_sooner_than_a_generic_error(self, pg):
        # The point of the separate threshold: weaker evidence than a 410, stronger
        # than a timeout.
        assert NOT_FOUND_DISABLE_THRESHOLD < FETCH_ERROR_DISABLE_THRESHOLD

    async def test_404_does_not_defer_the_feed(self, pg):
        # No Retry-After semantics on a 404 — the scheduler's error backoff paces it.
        feed = await _feed(pg, status="active", fetch_error_count=0)
        await _fail(pg, feed, _http_error(404))
        assert feed.retry_after_until is None


class TestCountersAreIndependent:
    async def test_a_block_does_not_reset_the_error_count(self, pg):
        feed = await _feed(pg, status="error", fetch_error_count=2, block_count=0)
        await _fail(pg, feed, _http_error(429))
        assert feed.fetch_error_count == 2
        assert feed.block_count == 1

    async def test_an_error_does_not_reset_the_block_count(self, pg):
        feed = await _feed(pg, status="active", fetch_error_count=0, block_count=4)
        await _fail(pg, feed, httpx.ConnectTimeout("timed out"))
        assert feed.block_count == 4
        assert feed.fetch_error_count == 1

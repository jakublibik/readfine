"""Admin dashboard roll-up of fetch errors: one row per feed, not per attempt.

A broken feed writes a FetchLog on every attempt, so the dashboard groups by feed
and counts failures per window (24h / 7d / 30d). The row carries the feed's live
state, so a feed that has fetched fine since its last logged failure still shows up
in the window but reads as active.

Runs against the real (dev) database inside a transaction that is always rolled
back; scoped to throwaway feeds. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.feed import Feed
from app.models.fetch_log import FetchLog
from app.services.admin_service import list_feed_fetch_errors

# A window in the past, so rows already in the dev database (whose logs are dated
# around the real "now") fall outside it and the assertions stay deterministic.
NOW = datetime(2020, 6, 1, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(days=30)


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


async def _feed(session, *, status="error", fetch_error_count=0, block_count=0) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(
        feed_url=f"https://ex.invalid/{u}.xml", title=f"feed-{u[:6]}",
        status=status, fetch_error_count=fetch_error_count, block_count=block_count,
    )
    session.add(feed)
    await session.flush()
    return feed


async def _log(session, feed, *, ago, message="boom", http_status=None) -> None:
    session.add(FetchLog(
        feed_id=feed.id, failed_at=NOW - ago,
        error_message=message, http_status=http_status,
    ))
    await session.flush()


async def _rows(session, limit=20) -> dict[int, dict]:
    rows = await list_feed_fetch_errors(session, since=SINCE, now=NOW, limit=limit)
    return {r["feed"].id: r for r in rows}


async def test_repeated_failures_collapse_to_one_row_with_window_counts(pg):
    feed = await _feed(pg, fetch_error_count=3)
    for ago in (timedelta(hours=1), timedelta(hours=5), timedelta(hours=20)):
        await _log(pg, feed, ago=ago)
    await _log(pg, feed, ago=timedelta(days=3))
    await _log(pg, feed, ago=timedelta(days=20))

    row = (await _rows(pg))[feed.id]
    assert row["fails_24h"] == 3
    assert row["fails_7d"] == 4
    assert row["fails"] == 5
    assert row["last_failed_at"] == NOW - timedelta(hours=1)


async def test_failures_outside_the_window_are_not_counted(pg):
    feed = await _feed(pg)
    await _log(pg, feed, ago=timedelta(days=2))
    await _log(pg, feed, ago=timedelta(days=45))

    row = (await _rows(pg))[feed.id]
    assert row["fails"] == 1
    assert row["fails_7d"] == 1
    assert row["fails_24h"] == 0


async def test_row_shows_newest_message_and_live_feed_state(pg):
    # Failed twice, then fetched fine: the fetcher resets status and the counters,
    # so the admin sees the logged error next to a feed that is healthy again.
    feed = await _feed(pg, status="active", fetch_error_count=0)
    await _log(pg, feed, ago=timedelta(hours=9), message="older", http_status=500)
    await _log(pg, feed, ago=timedelta(hours=4), message="newest", http_status=503)

    row = (await _rows(pg))[feed.id]
    assert row["error_message"] == "newest"
    assert row["feed"].status == "active"
    assert row["fails_24h"] == 2


async def test_feeds_ordered_by_newest_failure_and_limited(pg):
    older = await _feed(pg)
    newer = await _feed(pg)
    oldest = await _feed(pg)
    await _log(pg, oldest, ago=timedelta(days=10))
    await _log(pg, older, ago=timedelta(days=2))
    await _log(pg, newer, ago=timedelta(minutes=30))

    ours = {older.id, newer.id, oldest.id}
    ordered = [r["feed"].id for r in await list_feed_fetch_errors(pg, since=SINCE, now=NOW, limit=100)
               if r["feed"].id in ours]
    assert ordered == [newer.id, older.id, oldest.id]

    # The limit cuts the tail, keeping the feeds that failed most recently.
    top = await list_feed_fetch_errors(pg, since=SINCE, now=NOW, limit=1)
    assert len(top) == 1
    assert top[0]["feed"].id == newer.id

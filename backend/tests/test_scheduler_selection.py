"""Real-DB test for the scheduler's due-feed selection (_select_due_feeds).

Verifies the actual SQL (interval/backoff arithmetic + the tick-independent due
rule) against Postgres, guarding against drift from the pure _feed_due_for_selection
mirror unit-tested in test_fetcher.py. Runs inside a transaction that is always
rolled back and scoped to throwaway feeds; skips if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher.scheduler import _select_due_feeds
from app.models.feed import Feed

# Fixed reference time; _select_due_feeds is parameterised on `now`, so seeding
# last_fetched_at relative to this is fully deterministic (independent of DB clock).
NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


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


async def _feed(session, *, interval=None, status="active", last_offset=None,
                error_count=0, retry_offset=None, subscribers=1) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(
        feed_url=f"https://ex.invalid/{u}.xml",
        title=f"f-{u[:6]}",
        subscriber_count=subscribers,
        fetch_interval_min=interval,
        status=status,
        fetch_error_count=error_count,
        last_fetched_at=None if last_offset is None else NOW + last_offset,
        retry_after_until=None if retry_offset is None else NOW + retry_offset,
    )
    session.add(feed)
    await session.flush()
    return feed


async def _selected_ids(session, minute: int = 0) -> set[int]:
    now = NOW.replace(minute=minute)
    feeds = await _select_due_feeds(session, now, default_interval=60, min_interval=15)
    return {f.id for f in feeds}


class TestSelectDueFeeds:
    async def test_due_hourly_selected_at_every_tick(self, pg):
        # Tick-independent: a due (2h stale) hourly feed is picked at :00/:15/:30/:45
        # alike — no wall-clock slot alignment forcing it onto the top of the hour.
        f = await _feed(pg, interval=60, last_offset=timedelta(hours=-2))
        for minute in (0, 15, 30, 45):
            assert f.id in await _selected_ids(pg, minute), f"minute={minute}"

    async def test_fresh_hourly_selected_at_no_tick(self, pg):
        f = await _feed(pg, interval=60, last_offset=timedelta(minutes=-1))
        for minute in (0, 15, 30, 45):
            assert f.id not in await _selected_ids(pg, minute), f"minute={minute}"

    async def test_hourly_due_exactly_after_interval(self, pg):
        # 61 min elapsed on a 60-min feed → past due → selected.
        f = await _feed(pg, interval=60, last_offset=timedelta(minutes=-61))
        assert f.id in await _selected_ids(pg)

    async def test_hourly_within_grace_selected(self, pg):
        # 59 min elapsed: due in 1 min, inside the 2-min grace → selected now.
        f = await _feed(pg, interval=60, last_offset=timedelta(minutes=-59))
        assert f.id in await _selected_ids(pg)

    async def test_recent_hourly_not_fetched_early(self, pg):
        # Only 30 min elapsed on a 60-min feed → not due at any tick.
        f = await _feed(pg, interval=60, last_offset=timedelta(minutes=-30))
        assert f.id not in await _selected_ids(pg)

    async def test_15min_feed_due_after_interval(self, pg):
        f = await _feed(pg, interval=15, last_offset=timedelta(minutes=-20))
        assert f.id in await _selected_ids(pg, 15)
        assert f.id in await _selected_ids(pg, 30)

    async def test_error_feed_due_after_backoff(self, pg):
        # default_interval=60 → error backoff = max(15, 120) = 120 min; 3h stale → due.
        f = await _feed(pg, interval=60, status="error", last_offset=timedelta(hours=-3))
        assert f.id in await _selected_ids(pg, 15)

    async def test_error_feed_within_backoff_not_selected(self, pg):
        f = await _feed(pg, interval=60, status="error", last_offset=timedelta(minutes=-30))
        assert f.id not in await _selected_ids(pg, 15)

    async def test_retry_after_blocks_due_feed(self, pg):
        f = await _feed(pg, interval=60, last_offset=timedelta(hours=-2),
                        retry_offset=timedelta(minutes=30))
        assert f.id not in await _selected_ids(pg, 0)

    async def test_paused_and_unsubscribed_never_selected(self, pg):
        paused = await _feed(pg, interval=60, status="paused", last_offset=timedelta(hours=-2))
        no_subs = await _feed(pg, interval=60, last_offset=timedelta(hours=-2), subscribers=0)
        picked = await _selected_ids(pg, 0)
        assert paused.id not in picked
        assert no_subs.id not in picked

    async def test_never_fetched_selected(self, pg):
        f = await _feed(pg, interval=60, last_offset=None)
        assert f.id in await _selected_ids(pg, 30)

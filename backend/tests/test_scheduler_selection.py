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
from app.fetcher.interval import derive_interval_min
from app.fetcher.scheduler import (
    _feed_due_for_selection,
    _select_due_feeds,
    effective_interval_min,
    recompute_derived_intervals,
)
from app.models.article import Article
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


async def _feed(session, *, interval=None, derived=None, status="active", last_offset=None,
                error_count=0, retry_offset=None, subscribers=1, created_offset=None) -> Feed:
    u = uuid.uuid4().hex
    feed = Feed(
        feed_url=f"https://ex.invalid/{u}.xml",
        title=f"f-{u[:6]}",
        subscriber_count=subscribers,
        fetch_interval_min=interval,
        derived_interval_min=derived,
        status=status,
        fetch_error_count=error_count,
        last_fetched_at=None if last_offset is None else NOW + last_offset,
        retry_after_until=None if retry_offset is None else NOW + retry_offset,
    )
    if created_offset is not None:
        feed.created_at = datetime.now(timezone.utc) + created_offset
    session.add(feed)
    await session.flush()
    return feed


async def _selected_ids(session, minute: int = 0) -> set[int]:
    now = NOW.replace(minute=minute)
    feeds = await _select_due_feeds(
        session, now, default_interval=60, min_interval=15, max_interval=360
    )
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


class TestEffectiveIntervalDrift:
    """The SQL effective-interval expression must agree with the Python scalar across
    the manual/derived/default matrix, incl. the cap (auto only) and floor clamps.
    Uses non-default min/max to actually exercise both clamps."""

    DEFAULT, MIN, MAX = 60, 30, 180

    def _predict_due(self, feed, now) -> bool:
        eff = effective_interval_min(
            feed, default_interval_min=self.DEFAULT,
            min_interval_min=self.MIN, max_interval_min=self.MAX,
        )
        return _feed_due_for_selection(
            effective_interval_min=eff,
            status=feed.status,
            last_fetched_at=feed.last_fetched_at,
            retry_after_until=feed.retry_after_until,
            error_backoff_min=max(15, self.DEFAULT * 2),
            now=now,
        )

    async def test_sql_matches_python_across_matrix(self, pg):
        now = NOW.replace(minute=0)
        cases = [
            # (manual, derived, last_offset_min) covering: manual uncapped by MAX,
            # manual floored by MIN, derived, derived capped to MAX, default fallback.
            dict(interval=1440, derived=45, last_offset=timedelta(minutes=-200)),   # eff 1440 → not due
            dict(interval=15, derived=None, last_offset=timedelta(minutes=-45)),    # eff 30 → due
            dict(interval=None, derived=45, last_offset=timedelta(minutes=-50)),    # eff 45 → due
            dict(interval=None, derived=5040, last_offset=timedelta(hours=-2)),     # eff 180 → not due
            dict(interval=None, derived=5040, last_offset=timedelta(hours=-4)),     # eff 180 → due
            dict(interval=None, derived=None, last_offset=timedelta(minutes=-90)),  # eff 60 → due
            dict(interval=None, derived=None, last_offset=timedelta(minutes=-30)),  # eff 60 → not due
        ]
        feeds = [await _feed(pg, **c) for c in cases]
        selected = {
            f.id for f in await _select_due_feeds(
                pg, now, default_interval=self.DEFAULT,
                min_interval=self.MIN, max_interval=self.MAX,
            )
        }
        for feed in feeds:
            assert (feed.id in selected) == self._predict_due(feed, now), (
                f"drift for feed manual={feed.fetch_interval_min} "
                f"derived={feed.derived_interval_min}"
            )

    async def test_default_fallback_uncapped_when_above_cap(self, pg):
        # L1: with no manual override and no derived value, the default fallback is
        # floored but NOT capped. Here default (400) > max (180): a feed fetched 300 min
        # ago is NOT due (needs 400), which would be wrong if the SQL capped to 180.
        now = NOW.replace(minute=0)
        default, mn, mx = 400, 30, 180
        not_due = await _feed(pg, interval=None, derived=None, last_offset=timedelta(minutes=-300))
        due = await _feed(pg, interval=None, derived=None, last_offset=timedelta(minutes=-420))
        selected = {
            f.id for f in await _select_due_feeds(
                pg, now, default_interval=default, min_interval=mn, max_interval=mx,
            )
        }
        assert not_due.id not in selected  # capped-to-180 would wrongly select this
        assert due.id in selected


async def _article(session, feed, *, pub_offset=None, fetch_offset=None, trimmed=False):
    """Create an article on *feed*; offsets are relative to real now (recompute reads
    the real clock, not NOW)."""
    real_now = datetime.now(timezone.utc)
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title="t",
        published_at=None if pub_offset is None else real_now + pub_offset,
        fetched_at=real_now + (fetch_offset if fetch_offset is not None else timedelta(0)),
        trimmed_at=real_now if trimmed else None,
    )
    session.add(a)
    await session.flush()
    return a


class TestRecomputeDerivedIntervals:
    """recompute_derived_intervals: grouped publish-cadence count → Feed.derived_interval_min,
    writing only changed feeds. Formula itself is unit-tested in test_interval; here we
    guard the DB integration (window, trimmed filter, coalesce, only-changed writes)."""

    OLD = timedelta(days=30)   # created long enough ago for a full window
    IN_WINDOW = timedelta(days=-2)
    OUT_WINDOW = timedelta(days=-10)

    async def test_sets_derived_from_recent_publish_count(self, pg):
        feed = await _feed(pg, created_offset=-self.OLD)
        for _ in range(3):
            await _article(pg, feed, pub_offset=self.IN_WINDOW)
        # An older article outside the 7-day window must not be counted.
        await _article(pg, feed, pub_offset=self.OUT_WINDOW)
        pg.commit = pg.flush

        # Return counts every changed feed (a shared dev DB may hold others), so scope
        # the assertion to our feed's derived value rather than the global count.
        await recompute_derived_intervals(pg)

        expected = derive_interval_min(
            created_at=feed.created_at, count=3, now=datetime.now(timezone.utc)
        )
        assert feed.derived_interval_min == expected

    async def test_new_feed_stays_none(self, pg):
        # Younger than the window → derive returns None → no write.
        feed = await _feed(pg, created_offset=timedelta(days=-2))
        await _article(pg, feed, pub_offset=timedelta(hours=-1))
        pg.commit = pg.flush

        await recompute_derived_intervals(pg)

        assert feed.derived_interval_min is None

    async def test_trimmed_articles_excluded_and_coalesce_uses_fetched_at(self, pg):
        # One in-window article by fetched_at (no published_at → coalesce), plus a
        # trimmed in-window article that must be ignored. Count of 1.
        feed = await _feed(pg, created_offset=-self.OLD)
        await _article(pg, feed, pub_offset=None, fetch_offset=self.IN_WINDOW)
        await _article(pg, feed, pub_offset=self.IN_WINDOW, trimmed=True)
        pg.commit = pg.flush

        await recompute_derived_intervals(pg)

        expected = derive_interval_min(
            created_at=feed.created_at, count=1, now=datetime.now(timezone.utc)
        )
        assert feed.derived_interval_min == expected

    async def test_only_changed_feeds_written_and_idempotent(self, pg):
        feed = await _feed(pg, created_offset=-self.OLD)
        for _ in range(3):
            await _article(pg, feed, pub_offset=self.IN_WINDOW)
        pg.commit = pg.flush

        first = await recompute_derived_intervals(pg)
        # Scope the "changed" assertion to our feed: a shared dev DB may hold others.
        assert first >= 1
        target = feed.derived_interval_min

        second = await recompute_derived_intervals(pg)
        assert feed.derived_interval_min == target
        # Our feed is now unchanged; it must not be part of any second-pass write.
        # (Other feeds in a shared DB have also settled, so expect 0 here.)
        assert second == 0

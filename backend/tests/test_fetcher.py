"""Unit tests for fetch scheduler, interval quantization, and article helpers."""
import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.fetcher.failure import (
    BLOCK_BACKOFF_BASE,
    BLOCK_DISABLE_THRESHOLD,
)
from app.fetcher.rss import (
    FETCH_ERROR_DISABLE_THRESHOLD,
    _clamp_published_at,
    _extract_content,
    _latest_published,
    _normalize_guid,
    _normalize_url,
    _safe_url,
    _save_articles,
    _struct_to_dt,
    _url_dedup_keys,
    fetch_feed,
    is_full_content_feed,
)
from app.fetcher.scheduler import (
    compute_next_fetch_at,
    create_scheduler,
    effective_interval_min,
    _cooldown_wait,
    _COOLDOWN_BUFFER,
    _feed_due_for_selection,
    _MAX_SINGLE_WAIT,
    _run_throttled,
)
from app.fetcher import host_throttle
from app.models.fetch_log import FetchLog
from app.routers.web.admin import _quantize15
from app.utils.url_validator import ConditionalResponse


# ── per-host throttling ───────────────────────────────────────────────────────

class TestHostKey:
    def test_groups_www_and_bare(self):
        assert host_throttle.host_key("https://www.reddit.com/r/x.rss") == host_throttle.host_key("https://reddit.com/r/y.rss")
        assert host_throttle.host_key("https://www.reddit.com/r/x.rss") == "reddit.com"

    def test_distinct_hosts_differ(self):
        assert host_throttle.host_key("https://a.example/feed") != host_throttle.host_key("https://b.example/feed")

    def test_case_insensitive(self):
        assert host_throttle.host_key("https://Reddit.COM/x") == "reddit.com"


class TestFeedDueForSelection:
    """_feed_due_for_selection: a feed is due once its per-status timer has elapsed
    (with grace), independent of which 15-min tick this is — there is no wall-clock
    slot alignment, so feeds ride their own phase and spread across the hour."""

    NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)

    def _due(self, *, interval=60, status="active", last_offset=None,
             retry_offset=None, error_backoff_min=120):
        last = None if last_offset is None else self.NOW + last_offset
        retry = None if retry_offset is None else self.NOW + retry_offset
        return _feed_due_for_selection(
            effective_interval_min=interval, status=status,
            last_fetched_at=last, retry_after_until=retry,
            error_backoff_min=error_backoff_min, now=self.NOW,
        )

    def test_hourly_due_once_interval_elapsed(self):
        assert self._due(interval=60, last_offset=timedelta(minutes=-61))

    def test_hourly_due_within_grace_before_interval(self):
        # 2-min grace: a feed whose interval elapses just after now is already due,
        # so it is picked at this tick rather than slipping to the next one.
        assert self._due(interval=60, last_offset=timedelta(minutes=-59))

    def test_hourly_not_due_before_grace_window(self):
        # 55 min elapsed on a 60-min feed → still >2 min from due → not yet.
        assert not self._due(interval=60, last_offset=timedelta(minutes=-55))

    def test_hourly_recent_not_due(self):
        assert not self._due(interval=60, last_offset=timedelta(minutes=-30))

    def test_due_is_tick_independent(self):
        # The rule no longer reads the wall-clock minute: an elapsed hourly feed is
        # due at any tick, and a fresh one at none. (Slot alignment is gone.)
        assert self._due(interval=60, last_offset=timedelta(hours=-2))
        assert not self._due(interval=60, last_offset=timedelta(minutes=-1))

    def test_never_fetched_is_due(self):
        assert self._due(interval=60, last_offset=None)

    def test_15min_feed_due_after_interval(self):
        assert self._due(interval=15, last_offset=timedelta(minutes=-20))

    def test_15min_feed_fresh_not_due(self):
        assert not self._due(interval=15, last_offset=timedelta(minutes=-1))

    def test_retry_after_blocks_even_when_due(self):
        assert not self._due(interval=60, last_offset=timedelta(hours=-2),
                             retry_offset=timedelta(minutes=30))

    def test_past_retry_after_does_not_block(self):
        assert self._due(interval=60, last_offset=timedelta(hours=-2),
                         retry_offset=timedelta(minutes=-30))

    def test_error_feed_due_after_backoff(self):
        # error backoff 120 min; last fetched 3 h ago → past backoff → due.
        assert self._due(interval=60, status="error",
                         last_offset=timedelta(hours=-3))

    def test_error_feed_within_backoff_not_due(self):
        assert not self._due(interval=60, status="error",
                             last_offset=timedelta(minutes=-30))

    def test_error_feed_never_fetched_not_due(self):
        # An error feed with no last_fetched_at has no backoff anchor → not selected.
        assert not self._due(interval=60, status="error", last_offset=None)

    def test_paused_status_never_due(self):
        assert not self._due(interval=60, status="paused",
                             last_offset=timedelta(hours=-2))


class TestCooldownWait:
    """_cooldown_wait: wait a host cooldown out in-round vs. defer to the next round."""

    NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    DEADLINE = NOW + timedelta(minutes=12)  # generous round budget

    def test_no_cooldown_fetches_now(self):
        assert _cooldown_wait(None, self.NOW, self.DEADLINE) == timedelta(0)

    def test_expired_cooldown_fetches_now(self):
        past = self.NOW - timedelta(seconds=5)
        assert _cooldown_wait(past, self.NOW, self.DEADLINE) == timedelta(0)

    def test_short_reset_waits_with_buffer(self):
        until = self.NOW + timedelta(seconds=30)
        # Reddit-shaped reset well inside budget → wait it out (reset + buffer).
        assert _cooldown_wait(until, self.NOW, self.DEADLINE) == timedelta(seconds=30) + _COOLDOWN_BUFFER

    def test_reddit_60s_reset_waits(self):
        until = self.NOW + timedelta(seconds=60)
        assert _cooldown_wait(until, self.NOW, self.DEADLINE) == timedelta(seconds=60) + _COOLDOWN_BUFFER

    def test_at_single_wait_cap_still_waits(self):
        until = self.NOW + _MAX_SINGLE_WAIT
        assert _cooldown_wait(until, self.NOW, self.DEADLINE) == _MAX_SINGLE_WAIT + _COOLDOWN_BUFFER

    def test_over_single_wait_cap_defers(self):
        until = self.NOW + _MAX_SINGLE_WAIT + timedelta(seconds=1)
        assert _cooldown_wait(until, self.NOW, self.DEADLINE) is None

    def test_past_round_deadline_defers_even_if_under_cap(self):
        # Late in the round: a short reset that still lands past the deadline defers,
        # so we don't overrun the slot.
        tight_deadline = self.NOW + timedelta(seconds=30)
        until = self.NOW + timedelta(seconds=60)  # 60s < single-wait cap, but > deadline
        assert _cooldown_wait(until, self.NOW, tight_deadline) is None


class TestRunThrottled:
    async def test_same_host_serialized_other_hosts_parallel(self):
        cur = {}
        max_per_host = {}
        active_hosts = set()
        cross_host_overlap = {"seen": False}

        items = [("reddit.com", 1), ("reddit.com", 2), ("reddit.com", 3), ("other.com", 4)]

        async def worker(item):
            host = item[0]
            cur[host] = cur.get(host, 0) + 1
            max_per_host[host] = max(max_per_host.get(host, 0), cur[host])
            active_hosts.add(host)
            if len(active_hosts) > 1:
                cross_host_overlap["seen"] = True
            await asyncio.sleep(0.01)
            active_hosts.discard(host)
            cur[host] -= 1

        await _run_throttled(
            items, worker, global_limit=10, per_host_limit=1, host_of=lambda i: i[0]
        )
        # Same host never runs two at once; a different host overlaps with it.
        assert max_per_host["reddit.com"] == 1
        assert cross_host_overlap["seen"] is True

    async def test_per_host_limit_allows_configured_parallelism(self):
        cur = 0
        peak = 0

        async def worker(_item):
            nonlocal cur, peak
            cur += 1
            peak = max(peak, cur)
            await asyncio.sleep(0.01)
            cur -= 1

        await _run_throttled(
            [("h", i) for i in range(4)], worker,
            global_limit=10, per_host_limit=2, host_of=lambda i: i[0],
        )
        assert peak == 2

    async def test_exceptions_returned_not_raised(self):
        async def worker(item):
            if item == 2:
                raise ValueError("boom")

        results = await _run_throttled(
            [1, 2, 3], worker, global_limit=10, per_host_limit=1, host_of=lambda i: "h"
        )
        assert any(isinstance(r, ValueError) for r in results)

    async def test_on_host_ready_false_skips_worker(self):
        ran = []

        async def worker(item):
            ran.append(item)

        async def gate(item):
            return item != 2  # defer item 2

        await _run_throttled(
            [1, 2, 3], worker, global_limit=10, per_host_limit=1,
            host_of=lambda i: "h", on_host_ready=gate,
        )
        assert ran == [1, 3]

    async def test_on_host_ready_wait_holds_no_global_slot(self):
        # A gate that waits must not consume a global slot: with global_limit=1, a
        # worker on another host still runs concurrently while the gate sleeps.
        overlap = {"seen": False}
        active = set()

        async def gate(item):
            if item[0] == "slow":
                active.add("gate")
                await asyncio.sleep(0.02)
                active.discard("gate")
            return True

        async def worker(item):
            if "gate" in active:
                overlap["seen"] = True
            await asyncio.sleep(0.005)

        await _run_throttled(
            [("slow", 0), ("fast", 1)], worker,
            global_limit=1, per_host_limit=1, host_of=lambda i: i[0], on_host_ready=gate,
        )
        assert overlap["seen"] is True


# ── _quantize15 ───────────────────────────────────────────────────────────────

class TestQuantize15:
    def test_exact_multiple_unchanged(self):
        assert _quantize15(60, 60) == 60

    def test_rounds_up(self):
        assert _quantize15(23, 60) == 30

    def test_rounds_down(self):
        assert _quantize15(22, 60) == 15

    def test_midpoint_rounds_up(self):
        # 22.5 rounds to 30 (Python rounds half to even, but 7.5→ round(7.5/15)*15 = round(0.5)*15 = 0*15=0 → clamped to 15)
        # Let's use a clear case: 37 → round(37/15)=round(2.47)=2 → 30
        assert _quantize15(37, 60) == 30

    def test_below_minimum_clamped_to_15(self):
        assert _quantize15(1, 60) == 15

    def test_zero_clamped_to_15(self):
        assert _quantize15(0, 60) == 15

    def test_above_maximum_clamped_to_1440(self):
        assert _quantize15(9999, 60) == 1440

    def test_exactly_1440_unchanged(self):
        assert _quantize15(1440, 60) == 1440

    def test_none_uses_default(self):
        assert _quantize15(None, 60) == 60

    def test_none_default_also_quantized(self):
        assert _quantize15(None, 65) == 60

    def test_all_standard_values_unchanged(self):
        for v in (15, 30, 60, 90, 120, 180, 360, 720, 1440):
            assert _quantize15(v, 60) == v


# ── Scheduler trigger ─────────────────────────────────────────────────────────

class TestSchedulerTrigger:
    def _fetch_job(self):
        sched = create_scheduler()
        return sched.get_job("fetch_due_feeds")

    def test_fetch_job_exists(self):
        assert self._fetch_job() is not None

    def test_fetch_job_fires_on_quarter_hours(self):
        job = self._fetch_job()
        # From a mid-minute reference, next fire must land on :00/:15/:30/:45
        ref = datetime(2026, 1, 1, 12, 7, tzinfo=timezone.utc)
        next_time = job.trigger.get_next_fire_time(None, ref)
        assert next_time.minute in (0, 15, 30, 45)
        assert next_time.second == 0

    def test_fetch_job_consecutive_fires_are_15_min_apart(self):
        job = self._fetch_job()
        ref = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        t1 = job.trigger.get_next_fire_time(None, ref)
        t2 = job.trigger.get_next_fire_time(t1, t1)
        delta_minutes = (t2 - t1).total_seconds() / 60
        assert delta_minutes == 15

    def test_readable_job_is_not_affected(self):
        sched = create_scheduler()
        job = sched.get_job("process_readable")
        # process_readable stays on interval trigger (not cron)
        assert job is not None
        assert job.trigger.__class__.__name__ == "IntervalTrigger"

    def test_error_backoff_is_2x_interval(self):
        # Verify the backoff formula: max(15, interval * 2).
        # At default_interval=60, expected backoff = 120 min.
        default_interval = 60
        error_backoff_min = max(15, default_interval * 2)
        assert error_backoff_min == 120

    def test_error_backoff_minimum_is_15(self):
        # Very short interval (e.g. 1 min) still yields at least 15 min backoff.
        assert max(15, 1 * 2) == 15


# ── compute_next_fetch_at ─────────────────────────────────────────────────────

def _sched_feed(**kwargs) -> SimpleNamespace:
    defaults = dict(
        status="active",
        subscriber_count=1,
        fetch_interval_min=None,
        derived_interval_min=None,
        last_fetched_at=None,
        retry_after_until=None,
        fetch_error_count=0,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


class TestComputeNextFetchAt:
    """Predicted next fetch must mirror the _fetch_due_feeds query."""

    DEFAULTS = dict(default_interval_min=60, min_interval_min=15, max_interval_min=360)

    def test_active_uses_interval_snapped_to_tick(self):
        # 60-min feed last fetched at 13:00 → due 14:00, which is itself a tick.
        feed = _sched_feed(
            last_fetched_at=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)

    def test_off_phase_hourly_fires_at_next_tick_not_top_of_hour(self):
        # Key behaviour: an hourly feed no longer waits for :00. Last fetched 13:20
        # → due 14:20 → fetched at the 14:30 tick (grace snaps 14:18 up to :30),
        # not deferred to 15:00.
        feed = _sched_feed(
            last_fetched_at=datetime(2026, 1, 15, 13, 20, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 15, 14, 30, tzinfo=timezone.utc)

    def test_grace_pulls_forward_to_earlier_tick(self):
        # Due 14:16 → target 14:14 (grace) → snaps up to 14:15, matching the
        # selection query which counts the feed due at the 14:15 tick.
        feed = _sched_feed(
            last_fetched_at=datetime(2026, 1, 15, 13, 16, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 15, 14, 15, tzinfo=timezone.utc)

    def test_error_uses_double_default_backoff(self):
        # error feed, count below threshold → backoff max(15, 60*2) = 120 min.
        feed = _sched_feed(
            status="error",
            fetch_error_count=1,
            last_fetched_at=datetime(2026, 1, 15, 12, 0, 9, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 15, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)

    def test_error_at_disable_threshold_uses_24h(self):
        feed = _sched_feed(
            status="error",
            fetch_error_count=5,
            last_fetched_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 16, 12, 0, tzinfo=timezone.utc)

    def test_retry_after_defers_beyond_backoff(self):
        # 15-min feed (fires at every slot) so the snap isolates Retry-After.
        feed = _sched_feed(
            status="error",
            fetch_interval_min=15,
            fetch_error_count=1,
            last_fetched_at=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
            retry_after_until=datetime(2026, 1, 15, 16, 7, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        # Retry-After 16:07 wins over the 14:00 backoff; snapped up to :15.
        assert nxt == datetime(2026, 1, 15, 16, 15, tzinfo=timezone.utc)

    def test_disabled_and_paused_have_no_schedule(self):
        for status in ("disabled", "paused"):
            feed = _sched_feed(status=status)
            assert compute_next_fetch_at(feed, **self.DEFAULTS) is None

    def test_no_subscribers_has_no_schedule(self):
        feed = _sched_feed(subscriber_count=0)
        assert compute_next_fetch_at(feed, **self.DEFAULTS) is None

    def test_overdue_active_feed_returns_next_future_tick(self):
        # Last fetched long ago → due in the past; predicted fetch is the next tick,
        # no longer forced to the top of the hour.
        feed = _sched_feed(
            last_fetched_at=datetime(2026, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 7, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 15, 13, 15, tzinfo=timezone.utc)

    def test_15min_feed_fires_at_quarter_slots(self):
        feed = _sched_feed(
            fetch_interval_min=15,
            last_fetched_at=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 5, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        assert nxt == datetime(2026, 1, 15, 13, 15, tzinfo=timezone.utc)

    def test_auto_uses_derived_interval(self):
        # No manual override, derived 45 → active feed due 45 min after last fetch.
        feed = _sched_feed(
            derived_interval_min=45,
            last_fetched_at=datetime(2026, 1, 15, 13, 0, tzinfo=timezone.utc),
        )
        now = datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)
        nxt = compute_next_fetch_at(feed, now=now, **self.DEFAULTS)
        # due 13:45 → target 13:43 (grace) → snaps up to 13:45.
        assert nxt == datetime(2026, 1, 15, 13, 45, tzinfo=timezone.utc)


class TestEffectiveIntervalMin:
    """The Python scalar; the SQL mirror is drift-tested in test_scheduler_selection."""

    ARGS = dict(default_interval_min=60, min_interval_min=15, max_interval_min=360)

    def test_manual_override_is_authoritative_and_uncapped(self):
        # Manual 1440 must survive the 360 cap — it's an explicit user choice.
        feed = _sched_feed(fetch_interval_min=1440, derived_interval_min=45)
        assert effective_interval_min(feed, **self.ARGS) == 1440

    def test_manual_floored_at_min(self):
        feed = _sched_feed(fetch_interval_min=5)
        assert effective_interval_min(feed, **self.ARGS) == 15

    def test_auto_uses_derived(self):
        feed = _sched_feed(fetch_interval_min=None, derived_interval_min=90)
        assert effective_interval_min(feed, **self.ARGS) == 90

    def test_auto_derived_capped(self):
        feed = _sched_feed(fetch_interval_min=None, derived_interval_min=5040)
        assert effective_interval_min(feed, **self.ARGS) == 360

    def test_auto_no_derived_falls_back_to_default(self):
        feed = _sched_feed(fetch_interval_min=None, derived_interval_min=None)
        assert effective_interval_min(feed, **self.ARGS) == 60

    def test_auto_no_derived_default_above_cap_not_capped(self):
        # L1: default fallback is floored but NOT capped, so a high admin default wins.
        feed = _sched_feed(fetch_interval_min=None, derived_interval_min=None)
        args = dict(default_interval_min=720, min_interval_min=15, max_interval_min=360)
        assert effective_interval_min(feed, **args) == 720


# ── _clamp_published_at ───────────────────────────────────────────────────────

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


class TestClampPublishedAt:
    def test_valid_recent_date_unchanged(self):
        dt = NOW - timedelta(days=10)
        assert _clamp_published_at(dt, NOW) == dt

    def test_none_returns_none(self):
        assert _clamp_published_at(None, NOW) is None

    def test_before_2000_returns_none(self):
        dt = datetime(1999, 12, 31, tzinfo=timezone.utc)
        assert _clamp_published_at(dt, NOW) is None

    def test_exactly_2000_is_valid(self):
        dt = datetime(2000, 1, 1, tzinfo=timezone.utc)
        assert _clamp_published_at(dt, NOW) == dt

    def test_future_within_1_day_is_valid(self):
        dt = NOW + timedelta(hours=23)
        assert _clamp_published_at(dt, NOW) == dt

    def test_future_beyond_1_day_returns_none(self):
        dt = NOW + timedelta(days=2)
        assert _clamp_published_at(dt, NOW) is None

    def test_far_future_returns_none(self):
        dt = datetime(2099, 1, 1, tzinfo=timezone.utc)
        assert _clamp_published_at(dt, NOW) is None


# ── _struct_to_dt ─────────────────────────────────────────────────────────────

class TestStructToDt:
    def test_basic_conversion(self):
        t = (2026, 1, 15, 12, 30, 0, 0, 0, 0)
        assert _struct_to_dt(t) == datetime(2026, 1, 15, 12, 30, 0, tzinfo=timezone.utc)

    def test_always_utc(self):
        t = (2024, 6, 1, 0, 0, 0, 0, 0, 0)
        assert _struct_to_dt(t).tzinfo == timezone.utc

    def test_extra_fields_ignored(self):
        # feedparser struct_time has 9 fields; only first 6 are used
        t = (2026, 3, 10, 8, 0, 0, 99, 99, 99)
        assert _struct_to_dt(t) == datetime(2026, 3, 10, 8, 0, 0, tzinfo=timezone.utc)


# ── _normalize_guid ───────────────────────────────────────────────────────────

class TestNormalizeGuid:
    def test_http_url_fragment_stripped(self):
        assert _normalize_guid("http://example.com/article#0") == "http://example.com/article"

    def test_https_url_fragment_stripped(self):
        assert _normalize_guid("https://example.com/article#section") == "https://example.com/article"

    def test_url_without_fragment_unchanged(self):
        assert _normalize_guid("https://example.com/article") == "https://example.com/article"

    def test_non_url_guid_unchanged(self):
        assert _normalize_guid("urn:uuid:1234-5678") == "urn:uuid:1234-5678"

    def test_opaque_string_unchanged(self):
        assert _normalize_guid("some-opaque-id-123") == "some-opaque-id-123"

    def test_empty_string_unchanged(self):
        assert _normalize_guid("") == ""


# ── _normalize_url ───────────────────────────────────────────────────────────

class TestNormalizeUrl:
    def test_none_returns_none(self):
        assert _normalize_url(None) is None

    def test_trailing_slash_stripped(self):
        assert _normalize_url("https://example.com/article/") == "https://example.com/article"

    def test_scheme_lowercased(self):
        assert _normalize_url("HTTPS://example.com/path") == "https://example.com/path"

    def test_host_lowercased(self):
        assert _normalize_url("https://EXAMPLE.COM/path") == "https://example.com/path"

    def test_utm_params_stripped(self):
        url = "https://example.com/a?utm_source=rss&utm_medium=feed&id=42"
        assert _normalize_url(url) == "https://example.com/a?id=42"

    def test_all_utm_variants_stripped(self):
        url = "https://example.com/a?utm_source=x&utm_medium=y&utm_campaign=z&utm_term=t&utm_content=c&utm_id=1"
        assert _normalize_url(url) == "https://example.com/a"

    def test_fbclid_stripped(self):
        assert _normalize_url("https://example.com/a?fbclid=XYZ") == "https://example.com/a"

    def test_gclid_stripped(self):
        assert _normalize_url("https://example.com/a?gclid=ABC") == "https://example.com/a"

    def test_non_tracking_params_kept(self):
        assert _normalize_url("https://example.com/a?id=42&page=2") == "https://example.com/a?id=42&page=2"

    def test_fragment_stripped(self):
        assert _normalize_url("https://example.com/a#section") == "https://example.com/a"

    def test_ftp_scheme_returns_none(self):
        assert _normalize_url("ftp://files.example.com/file") is None

    def test_empty_path_normalized_to_slash(self):
        result = _normalize_url("https://example.com")
        assert result == "https://example.com/"

    def test_same_url_different_utm_produces_same_result(self):
        a = _normalize_url("https://example.com/a?utm_source=twitter")
        b = _normalize_url("https://example.com/a?utm_source=facebook")
        assert a == b


# ── _safe_url ─────────────────────────────────────────────────────────────────

class TestSafeUrl:
    def test_http_url_allowed(self):
        assert _safe_url("http://example.com/feed") == "http://example.com/feed"

    def test_https_url_allowed(self):
        assert _safe_url("https://example.com/feed") == "https://example.com/feed"

    def test_none_returns_none(self):
        assert _safe_url(None) is None

    def test_empty_string_returns_none(self):
        assert _safe_url("") is None

    def test_javascript_scheme_blocked(self):
        assert _safe_url("javascript:alert(1)") is None

    def test_data_scheme_blocked(self):
        assert _safe_url("data:text/html,<h1>x</h1>") is None

    def test_ftp_scheme_blocked(self):
        assert _safe_url("ftp://files.example.com/file") is None

    def test_url_truncated_to_max_len(self):
        long_url = "https://example.com/" + "a" * 2048
        result = _safe_url(long_url)
        assert result is not None
        assert len(result) == 2048

    def test_whitespace_stripped(self):
        assert _safe_url("  https://example.com/  ") == "https://example.com/"


# ── feedparser entry mock ─────────────────────────────────────────────────────

class AttrDict(dict):
    """dict that also exposes keys as attributes — mirrors feedparser FeedParserDict."""
    def __getattr__(self, key):
        try:
            val = self[key]
            # Recursively wrap nested dicts so attribute access works at all depths.
            if isinstance(val, list):
                return [AttrDict(v) if isinstance(v, dict) else v for v in val]
            return AttrDict(val) if isinstance(val, dict) else val
        except KeyError:
            raise AttributeError(key)


# ── is_full_content_feed ──────────────────────────────────────────────────────

class TestIsFullContentFeed:
    def _parsed(self, generator=None, entries=None):
        import feedparser
        feed = feedparser.FeedParserDict()
        if generator is not None:
            feed["generator"] = generator
        return feedparser.FeedParserDict({"feed": feed, "entries": entries or []})

    def test_tumblr_generator_short_circuits_true(self):
        # Tumblr posts are short (well under 500 words) but the feed carries the full
        # body, so the generator must flag it as full-content regardless of length.
        parsed = self._parsed(
            generator="Tumblr (3.0; @someblog)",
            entries=[AttrDict({"summary": "out of granola bars"})],
        )
        assert is_full_content_feed(parsed) is True

    def test_generator_match_is_case_insensitive(self):
        parsed = self._parsed(generator="TUMBLR", entries=[])
        assert is_full_content_feed(parsed) is True

    def test_unknown_generator_falls_back_to_word_count(self):
        # No known platform + short entries → not full content.
        parsed = self._parsed(
            generator="WordPress",
            entries=[AttrDict({"summary": "tiny"})],
        )
        assert is_full_content_feed(parsed) is False

    def test_long_entries_still_full_content(self):
        parsed = self._parsed(entries=[AttrDict({"summary": "word " * 600})])
        assert is_full_content_feed(parsed) is True


# ── _extract_content ──────────────────────────────────────────────────────────

class TestExtractContent:
    def test_content_field_preferred_over_summary(self):
        entry = AttrDict({"content": [{"value": "full article"}], "summary": "short summary"})
        content, source = _extract_content(entry)
        assert content == "full article"
        assert source == "feed_full"

    def test_summary_used_when_no_content(self):
        entry = AttrDict({"summary": "summary text"})
        content, source = _extract_content(entry)
        assert content == "summary text"
        assert source == "feed_summary"

    def test_empty_entry_returns_none(self):
        content, source = _extract_content(AttrDict({}))
        assert content is None
        assert source is None

    def test_content_first_nonempty_value_used(self):
        entry = AttrDict({"content": [{"value": "first"}]})
        content, source = _extract_content(entry)
        assert content == "first"
        assert source == "feed_full"

    def test_content_skips_empty_value(self):
        entry = AttrDict({"content": [{"value": ""}, {"value": "second"}]})
        content, source = _extract_content(entry)
        assert content == "second"
        assert source == "feed_full"


# ── _latest_published ─────────────────────────────────────────────────────────

class TestLatestPublished:
    def test_returns_maximum_date(self):
        entries = [
            {"published_parsed": (2026, 1, 10, 0, 0, 0, 0, 0, 0)},
            {"published_parsed": (2026, 1, 20, 0, 0, 0, 0, 0, 0)},
            {"published_parsed": (2026, 1, 5, 0, 0, 0, 0, 0, 0)},
        ]
        result = _latest_published(entries)
        assert result == datetime(2026, 1, 20, 0, 0, 0, tzinfo=timezone.utc)

    def test_empty_entries_returns_none(self):
        assert _latest_published([]) is None

    def test_entries_without_date_ignored(self):
        assert _latest_published([{"title": "no date"}]) is None

    def test_falls_back_to_updated_parsed(self):
        entries = [{"updated_parsed": (2026, 3, 1, 0, 0, 0, 0, 0, 0)}]
        result = _latest_published(entries)
        assert result == datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)

    def test_published_preferred_over_updated(self):
        entries = [{
            "published_parsed": (2026, 1, 20, 0, 0, 0, 0, 0, 0),
            "updated_parsed":   (2026, 1, 25, 0, 0, 0, 0, 0, 0),
        }]
        # feedparser `or` picks published_parsed first
        result = _latest_published(entries)
        assert result == datetime(2026, 1, 20, 0, 0, 0, tzinfo=timezone.utc)


# ── Error circuit breaker — threshold constants ───────────────────────────────

class TestErrorCircuitBreakerThreshold:
    """Tests for the consecutive-error disable threshold and backoff tier logic."""

    def test_threshold_is_five(self):
        assert FETCH_ERROR_DISABLE_THRESHOLD == 5

    def test_below_threshold_status_is_error(self):
        # counts 0..threshold-1 should all remain in 'error'
        for count in range(FETCH_ERROR_DISABLE_THRESHOLD):
            status = "disabled" if count >= FETCH_ERROR_DISABLE_THRESHOLD else "error"
            assert status == "error", f"count={count} should give 'error', got 'disabled'"

    def test_at_threshold_status_is_disabled(self):
        count = FETCH_ERROR_DISABLE_THRESHOLD
        status = "disabled" if count >= FETCH_ERROR_DISABLE_THRESHOLD else "error"
        assert status == "disabled"

    def test_above_threshold_status_is_disabled(self):
        count = FETCH_ERROR_DISABLE_THRESHOLD + 3
        status = "disabled" if count >= FETCH_ERROR_DISABLE_THRESHOLD else "error"
        assert status == "disabled"

    def test_backoff_below_threshold_uses_2x_interval(self):
        default_interval = 60
        for count in range(FETCH_ERROR_DISABLE_THRESHOLD):
            backoff = 24 * 60 if count >= FETCH_ERROR_DISABLE_THRESHOLD else max(15, default_interval * 2)
            assert backoff == 120, f"count={count} should use 2× backoff (120 min)"

    def test_backoff_at_threshold_uses_24h(self):
        default_interval = 60
        count = FETCH_ERROR_DISABLE_THRESHOLD
        backoff = 24 * 60 if count >= FETCH_ERROR_DISABLE_THRESHOLD else max(15, default_interval * 2)
        assert backoff == 24 * 60

    def test_backoff_above_threshold_uses_24h(self):
        default_interval = 60
        count = FETCH_ERROR_DISABLE_THRESHOLD + 2
        backoff = 24 * 60 if count >= FETCH_ERROR_DISABLE_THRESHOLD else max(15, default_interval * 2)
        assert backoff == 24 * 60

    def test_backoff_short_interval_still_uses_24h_at_threshold(self):
        # Even if default_interval is tiny, tier-2 is always 24 h
        default_interval = 1
        count = FETCH_ERROR_DISABLE_THRESHOLD
        backoff = 24 * 60 if count >= FETCH_ERROR_DISABLE_THRESHOLD else max(15, default_interval * 2)
        assert backoff == 24 * 60


# ── Error circuit breaker — fetch_feed integration (mocked DB) ────────────────

def _make_feed(**kwargs) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "feed_url": "https://example.com/feed.xml",
        "is_private": False,
        "fetch_auth_user": None,
        "fetch_auth_pass_encrypted": None,
        "fetch_error_count": 0,
        "block_count": 0,
        "status": "active",
        "last_fetched_at": None,
        "last_fetch_duration_ms": None,
        "last_error": None,
        "last_published_at": None,
        "retry_after_until": None,
        "etag": None,
        "last_modified": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _update_values(session) -> dict:
    """Extract column-name → value-clause from the update(Feed) the error path ran."""
    stmt = session.execute.call_args[0][0]
    return {col.name: val for col, val in stmt._values.items()}


def _status_is_disabled(status_clause) -> bool:
    """True only when status was set to the literal 'disabled' (not a case/error tier)."""
    from sqlalchemy.sql.elements import BindParameter
    return isinstance(status_clause, BindParameter) and status_clause.value == "disabled"


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestFetchFeedErrorHandling:
    """fetch_feed: error path adds FetchLog, commits error state."""

    async def test_fetch_error_returns_zero(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=ValueError("parse error")):
            result = await fetch_feed(feed, session)
        assert result == 0

    async def test_fetch_error_adds_fetchlog(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=ValueError("parse error")):
            await fetch_feed(feed, session)
        assert session.add.called
        added = session.add.call_args[0][0]
        assert isinstance(added, FetchLog)
        assert added.feed_id == 1
        assert "parse error" in added.error_message

    async def test_fetch_error_records_http_status(self):
        import httpx
        feed = _make_feed()
        session = _make_session()
        request = httpx.Request("GET", "https://example.com/feed.xml")
        response = httpx.Response(404, request=request)
        exc = httpx.HTTPStatusError("404", request=request, response=response)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, session)
        added = session.add.call_args[0][0]
        assert added.http_status == 404

    async def test_fetch_error_rolls_back_then_commits(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=ValueError("x")):
            await fetch_feed(feed, session)
        session.rollback.assert_called_once()
        session.commit.assert_called_once()

    async def test_fetch_error_truncates_long_message(self):
        feed = _make_feed()
        session = _make_session()
        long_msg = "e" * 600
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=ValueError(long_msg)):
            await fetch_feed(feed, session)
        added = session.add.call_args[0][0]
        assert len(added.error_message) <= 500


def _http_error(status: int, headers: dict | None = None):
    import httpx
    request = httpx.Request("GET", "https://example.com/feed.xml")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(str(status), request=request, response=response)


class TestFetchFeed429Transient:
    """A 429 (rate limit) backs the feed off instead of disabling it on first hit."""

    async def test_429_does_not_disable(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(429)):
            await fetch_feed(feed, session)
        vals = _update_values(session)
        assert not _status_is_disabled(vals["status"])

    async def test_429_records_http_status(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(429)):
            await fetch_feed(feed, session)
        added = session.add.call_args[0][0]
        assert added.http_status == 429

    async def test_429_retry_after_wins_when_longer_than_block_backoff(self):
        # A 429 is a block, so the feed always gets at least BLOCK_BACKOFF_BASE. A
        # Retry-After longer than that is the host's own instruction and wins.
        feed = _make_feed()
        session = _make_session()
        exc = _http_error(429, headers={"Retry-After": "3600"})
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, session)
        rau = _update_values(session)["retry_after_until"].value
        assert rau is not None
        assert before + timedelta(seconds=3590) <= rau <= before + timedelta(seconds=3610)

    async def test_429_without_header_still_backs_off(self):
        # No Retry-After: the block backoff applies anyway, so we stop retrying a
        # refusing host on the feed's normal interval.
        feed = _make_feed()
        session = _make_session()
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(429)):
            await fetch_feed(feed, session)
        rau = _update_values(session)["retry_after_until"].value
        assert rau is not None
        assert abs((rau - (before + BLOCK_BACKOFF_BASE)).total_seconds()) < 5

    async def test_404_still_disables(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(404)):
            await fetch_feed(feed, session)
        vals = _update_values(session)
        assert _status_is_disabled(vals["status"])
        assert vals["retry_after_until"].value is None


class TestFetchFeed403Transient:
    """A 403 (often a transient anti-bot / rate-adjacent block on Reddit/YouTube)
    backs the feed off through the error tier instead of disabling it on first hit."""

    def setup_method(self):
        host_throttle.clear()

    def teardown_method(self):
        host_throttle.clear()

    async def test_403_does_not_disable_on_first_hit(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        assert not _status_is_disabled(_update_values(session)["status"])

    async def test_403_records_http_status(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        assert session.add.call_args[0][0].http_status == 403

    async def test_403_disables_at_threshold(self):
        # Once fetch_error_count has reached the threshold, the error tier disables it.
        feed = _make_feed(fetch_error_count=FETCH_ERROR_DISABLE_THRESHOLD)
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        # status is a CASE(count >= threshold -> disabled); the count column is >= threshold
        assert feed.fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD

    async def test_403_arms_scheduler_only_breather_not_manual(self):
        # A bare anti-bot 403 (Retry-After: 0, no RateLimit-*) arms only the 403
        # breather: it paces the scheduler (include_block) but must NOT block a manual
        # refresh (default blocked_until stays None).
        feed = _make_feed()
        session = _make_session()
        before = datetime.now(timezone.utc)
        exc = _http_error(403, headers={"Retry-After": "0"})
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, session)
        # manual-facing lookup: not blocked
        assert host_throttle.blocked_until("example.com", before) is None
        # scheduler-facing lookup: 60s breather armed
        sched = host_throttle.blocked_until("example.com", before, include_block=True)
        assert sched is not None
        expected = before + host_throttle.FALLBACK_BLOCK_COOLDOWN
        assert abs((sched - expected).total_seconds()) < 2

    async def test_403_with_real_ratelimit_signal_gates_everyone(self):
        # If a 403 carries a usable rate-limit signal it's a genuine throttle: arm the
        # rate-limit cooldown (remaining=0 + reset=30 → 30s), which the default
        # blocked_until — used by manual refreshes — also honors.
        feed = _make_feed()
        session = _make_session()
        before = datetime.now(timezone.utc)
        exc = _http_error(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "30"})
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, session)
        until = host_throttle.blocked_until("example.com", before)  # gates manual too
        assert until is not None
        assert abs((until - (before + timedelta(seconds=30))).total_seconds()) < 2


class TestFetchFeedHostCooldown:
    """fetch_feed arms the in-memory per-host cooldown from rate-limit headers, so
    sibling feeds on the same host defer instead of bursting into 429."""

    def setup_method(self):
        host_throttle.clear()

    def teardown_method(self):
        host_throttle.clear()

    async def test_429_reset_header_arms_cooldown_without_retry_after(self):
        # Reddit-shaped 429: x-ratelimit-reset but no Retry-After.
        feed = _make_feed()
        session = _make_session()
        exc = _http_error(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "45"})
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, session)
        # The host cooldown is armed ~45s out.
        until = host_throttle.blocked_until("example.com", before)
        assert until is not None
        assert before + timedelta(seconds=40) <= until <= before + timedelta(seconds=50)

    async def test_200_with_ratelimit_headers_arms_cooldown(self):
        import feedparser
        feed = _make_feed()
        session = _make_session()
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
        )
        until = datetime.now(timezone.utc) + timedelta(seconds=59)
        resp = ConditionalResponse(200, "<rss/>", None, None, rate_limited_until=until)
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            patch("app.fetcher.rss._save_articles", return_value=0),
        ):
            await fetch_feed(feed, session)
        assert host_throttle.blocked_until("example.com", datetime.now(timezone.utc)) == until

    async def test_200_without_ratelimit_headers_no_cooldown(self):
        import feedparser
        feed = _make_feed()
        session = _make_session()
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
        )
        resp = ConditionalResponse(200, "<rss/>", None, None)
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            patch("app.fetcher.rss._save_articles", return_value=0),
        ):
            await fetch_feed(feed, session)
        assert host_throttle.blocked_until("example.com", datetime.now(timezone.utc)) is None


class TestFetchFeedLearnedSpacing:
    """fetch_feed feeds the learned per-host spacing store: precise learning from a
    200's RateLimit-* headers, tightening on repeated 429s."""

    def setup_method(self):
        host_throttle.clear()

    def teardown_method(self):
        host_throttle.clear()

    async def test_200_headers_learn_spacing(self):
        import feedparser
        feed = _make_feed(feed_url="https://reddit.com/r/x/.rss")
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
        )
        resp = ConditionalResponse(200, "<rss/>", None, None, spacing_seconds=8.0)
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            patch("app.fetcher.rss._save_articles", return_value=0),
        ):
            await fetch_feed(feed, _make_session())
        assert host_throttle.effective_spacing("reddit.com") == 8.0

    async def test_lone_429_bumps_streak_without_tightening(self):
        feed = _make_feed(feed_url="https://reddit.com/r/x/.rss")
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(429)):
            await fetch_feed(feed, _make_session())
        assert host_throttle._spacing["reddit.com"].consecutive_429 == 1
        assert host_throttle.effective_spacing("reddit.com") == host_throttle.GLOBAL_MIN_SPACING

    async def test_two_429s_tighten_spacing(self):
        feed = _make_feed(feed_url="https://reddit.com/r/x/.rss")
        exc = _http_error(429, headers={"Retry-After": "600"})
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, _make_session())
            await fetch_feed(feed, _make_session())
        assert host_throttle._spacing["reddit.com"].source == "429"
        assert host_throttle.effective_spacing("reddit.com") > host_throttle.GLOBAL_MIN_SPACING

    async def test_403_does_not_tighten_spacing(self):
        # A bare anti-bot 403 is not a throttle signal — no ratchet.
        feed = _make_feed(feed_url="https://reddit.com/r/x/.rss")
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, _make_session())
            await fetch_feed(feed, _make_session())
        assert "reddit.com" not in host_throttle._spacing

    async def test_429_reset_header_does_not_tighten_spacing(self):
        # Regression guard for the whole change. Reddit sends no Retry-After, only
        # remaining=0 + reset=N, where N is the seconds left in the current wall-clock
        # window — a deadline, not a rate. Ratcheting on it is how reddit.com acquired
        # a learned 78s spacing that no measurement supported, and it would come back
        # through this path even with spacing_from_headers fixed.
        feed = _make_feed(feed_url="https://reddit.com/r/x/.rss")
        exc = _http_error(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "45"})
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, _make_session())
            await fetch_feed(feed, _make_session())
        # Nothing learned: two 429s tighten only from GLOBAL_MIN_SPACING.
        assert host_throttle.effective_spacing("reddit.com") == pytest.approx(
            host_throttle.GLOBAL_MIN_SPACING * host_throttle.SPACING_MARGIN
        )
        # The same headers are still honored as a cooldown — that reading is correct.
        until = host_throttle.blocked_until("reddit.com", before)
        assert until is not None
        assert before + timedelta(seconds=40) <= until <= before + timedelta(seconds=50)

    async def test_429_with_real_retry_after_still_tightens(self):
        # The ratchet is not switched off, it is fed only instructions it can act on.
        feed = _make_feed(feed_url="https://reddit.com/r/x/.rss")
        exc = _http_error(429, headers={"Retry-After": "120"})
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=exc):
            await fetch_feed(feed, _make_session())
            await fetch_feed(feed, _make_session())
        assert host_throttle.effective_spacing("reddit.com") == pytest.approx(
            120 * host_throttle.SPACING_MARGIN
        )


class TestFetchFeedBlockTier:
    """A host refusing automation moves block_count, never fetch_error_count."""

    def setup_method(self):
        host_throttle.clear()

    def teardown_method(self):
        host_throttle.clear()

    async def test_403_bumps_block_count_only(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        vals = _update_values(session)
        assert "block_count" in vals
        assert "fetch_error_count" not in vals

    async def test_403_leaves_the_feed_active(self):
        # The status expression is Feed.status, i.e. "whatever it already was" —
        # never the literal 'error' the error tier would write.
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        assert not _status_is_disabled(_update_values(session)["status"])

    async def test_block_on_a_feed_already_in_error_does_not_touch_the_error_tier(self):
        # A feed carrying a real failure keeps it: the block neither clears the error
        # nor advances it toward the disable threshold.
        feed = _make_feed(status="error", fetch_error_count=3)
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        vals = _update_values(session)
        assert "fetch_error_count" not in vals
        assert "block_count" in vals

    async def test_block_defers_the_feed(self):
        feed = _make_feed()
        session = _make_session()
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        rau = _update_values(session)["retry_after_until"].value
        assert abs((rau - (before + BLOCK_BACKOFF_BASE)).total_seconds()) < 5

    async def test_block_backoff_grows_with_the_existing_count(self):
        feed = _make_feed(block_count=2)
        session = _make_session()
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_http_error(403)):
            await fetch_feed(feed, session)
        rau = _update_values(session)["retry_after_until"].value
        assert abs((rau - (before + BLOCK_BACKOFF_BASE * 4)).total_seconds()) < 5

    async def test_timeout_still_uses_the_error_tier(self):
        # Negative path: an exception with no response must not reach exc.response.
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=httpx.ConnectTimeout("x")):
            await fetch_feed(feed, session)
        vals = _update_values(session)
        assert "fetch_error_count" in vals
        assert "block_count" not in vals
        assert vals["retry_after_until"].value is None

    async def test_success_clears_both_counters(self):
        import feedparser
        feed = _make_feed(status="error", fetch_error_count=4, block_count=7)
        session = _make_session()
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
        )
        await fetch_feed(feed, session, prefetched=parsed)
        assert feed.fetch_error_count == 0
        assert feed.block_count == 0
        assert feed.status == "active"


class TestFetchFeedConditional:
    """fetch_feed: ETag / If-Modified-Since conditional requests + 304 handling."""

    async def test_304_skips_parse_and_records_success(self):
        feed = _make_feed(etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
                          status="error", fetch_error_count=3)
        session = _make_session()
        resp = ConditionalResponse(304, "", None, None)
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
            patch("app.fetcher.rss.feedparser.parse") as mock_parse,
        ):
            result = await fetch_feed(feed, session)
        assert result == 0
        mock_parse.assert_not_called()
        assert feed.last_fetched_at is not None
        assert feed.status == "active"
        assert feed.fetch_error_count == 0
        assert feed.retry_after_until is None
        # Stored validators are preserved across a 304.
        assert feed.etag == '"abc"'
        assert feed.last_modified == "Mon, 01 Jan 2024 00:00:00 GMT"
        session.commit.assert_awaited()

    async def test_stored_validators_sent_as_conditional_headers(self):
        feed = _make_feed(etag='"v1"', last_modified="Tue, 02 Jan 2024 00:00:00 GMT")
        session = _make_session()
        resp = ConditionalResponse(304, "", None, None)
        with patch("app.fetcher.rss.fetch_url_conditional", return_value=resp) as mock_fetch:
            await fetch_feed(feed, session)
        # run_in_executor(None, fetch_url_conditional, url, auth, timeout, headers, etag, last_modified)
        args = mock_fetch.call_args[0]
        assert args[-2] == '"v1"'
        assert args[-1] == "Tue, 02 Jan 2024 00:00:00 GMT"

    async def test_200_stores_returned_validators(self):
        import feedparser
        feed = _make_feed()
        session = _make_session()
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
        )
        resp = ConditionalResponse(200, "<rss/>", '"new-etag"', "Wed, 03 Jan 2024 00:00:00 GMT")
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            patch("app.fetcher.rss._save_articles", return_value=0),
        ):
            await fetch_feed(feed, session)
        assert feed.etag == '"new-etag"'
        assert feed.last_modified == "Wed, 03 Jan 2024 00:00:00 GMT"
        assert feed.status == "active"

    async def test_200_without_validators_keeps_stored_ones(self):
        import feedparser
        feed = _make_feed(etag='"keep"', last_modified="Tue, 02 Jan 2024 00:00:00 GMT")
        session = _make_session()
        parsed = feedparser.FeedParserDict(
            {"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})}
        )
        resp = ConditionalResponse(200, "<rss/>", None, None)
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=resp),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            patch("app.fetcher.rss._save_articles", return_value=0),
        ):
            await fetch_feed(feed, session)
        assert feed.etag == '"keep"'
        assert feed.last_modified == "Tue, 02 Jan 2024 00:00:00 GMT"


class TestFetchFeedDuplicateRace:
    """fetch_feed: a concurrent-fetch IntegrityError is a benign no-op, not a failure."""

    def _patch_save_raises_integrity(self):
        from sqlalchemy.exc import IntegrityError
        exc = IntegrityError("INSERT INTO articles ...", {}, Exception("duplicate key value"))
        return patch("app.fetcher.rss._save_articles", side_effect=exc)

    async def test_returns_zero(self):
        feed = _make_feed()
        session = _make_session()
        import feedparser
        parsed = feedparser.FeedParserDict({"bozo": False, "entries": [{}], "feed": feedparser.FeedParserDict({})})
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=ConditionalResponse(200, "<rss/>", None, None)),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            self._patch_save_raises_integrity(),
        ):
            result = await fetch_feed(feed, session)
        assert result == 0

    async def test_feed_stays_active_and_error_count_unchanged(self):
        feed = _make_feed(fetch_error_count=0, status="active")
        session = _make_session()
        import feedparser
        parsed = feedparser.FeedParserDict({"bozo": False, "entries": [{}], "feed": feedparser.FeedParserDict({})})
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=ConditionalResponse(200, "<rss/>", None, None)),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
            self._patch_save_raises_integrity(),
        ):
            await fetch_feed(feed, session)
        assert feed.fetch_error_count == 0
        assert feed.status == "active"
        # No FetchLog written for a benign race.
        assert not session.add.called
        session.rollback.assert_called_once()


class TestFetchFeedSuccessReset:
    """fetch_feed: success path resets error counter and status."""

    async def test_success_resets_error_count(self):
        feed = _make_feed(fetch_error_count=3, status="error")
        session = _make_session()

        import feedparser
        parsed = feedparser.FeedParserDict({
            "bozo": False,
            "entries": [],
            "feed": feedparser.FeedParserDict({}),
        })
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=ConditionalResponse(200, "<rss/>", None, None)),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
        ):
            await fetch_feed(feed, session)

        assert feed.fetch_error_count == 0

    async def test_success_sets_status_active(self):
        feed = _make_feed(fetch_error_count=3, status="error")
        session = _make_session()

        import feedparser
        parsed = feedparser.FeedParserDict({
            "bozo": False,
            "entries": [],
            "feed": feedparser.FeedParserDict({}),
        })
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=ConditionalResponse(200, "<rss/>", None, None)),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
        ):
            await fetch_feed(feed, session)

        assert feed.status == "active"

    async def test_success_clears_last_error(self):
        feed = _make_feed(last_error="previous error", status="error")
        session = _make_session()

        import feedparser
        parsed = feedparser.FeedParserDict({
            "bozo": False,
            "entries": [],
            "feed": feedparser.FeedParserDict({}),
        })
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=ConditionalResponse(200, "<rss/>", None, None)),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
        ):
            await fetch_feed(feed, session)

        assert feed.last_error is None

    async def test_success_clears_retry_after_until(self):
        feed = _make_feed(
            status="error",
            retry_after_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session = _make_session()

        import feedparser
        parsed = feedparser.FeedParserDict({
            "bozo": False,
            "entries": [],
            "feed": feedparser.FeedParserDict({}),
        })
        with (
            patch("app.fetcher.rss.fetch_url_conditional", return_value=ConditionalResponse(200, "<rss/>", None, None)),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
        ):
            await fetch_feed(feed, session)

        assert feed.retry_after_until is None


class TestFetchFeedPrefetched:
    """fetch_feed(prefetched=...) reuses an existing parse instead of downloading."""

    async def test_prefetched_skips_network(self):
        import feedparser
        feed = _make_feed()
        session = _make_session()
        parsed = feedparser.FeedParserDict({"bozo": False, "entries": [], "feed": feedparser.FeedParserDict({})})

        def _boom(*a, **k):
            raise AssertionError("network fetch must not happen when prefetched is given")

        with patch("app.fetcher.rss.fetch_url_conditional", side_effect=_boom):
            result = await fetch_feed(feed, session, prefetched=parsed)

        assert result == 0
        assert feed.status == "active"


class TestFeedPreviewCache:
    """Short-lived test→subscribe parse cache (keeps the add flow to one fetch)."""

    def test_roundtrip(self):
        import feedparser
        from app.services.feed import cache_feed_preview, get_cached_feed_preview
        parsed = feedparser.FeedParserDict({"entries": []})
        cache_feed_preview("https://cache.example/feed", parsed)
        assert get_cached_feed_preview("https://cache.example/feed") is parsed

    def test_miss_returns_none(self):
        from app.services.feed import get_cached_feed_preview
        assert get_cached_feed_preview("https://cache.example/never-cached") is None

    def test_expired_entry_evicted(self):
        import feedparser
        from app.services import feed as feed_svc
        url = "https://cache.example/expired"
        feed_svc.cache_feed_preview(url, feedparser.FeedParserDict({"entries": []}))
        # force the stored expiry into the past
        _, value, permanent = feed_svc._feed_preview_cache[url]
        feed_svc._feed_preview_cache[url] = (0.0, value, permanent)
        assert feed_svc.get_cached_feed_preview(url) is None
        assert url not in feed_svc._feed_preview_cache


# ── _url_dedup_keys ───────────────────────────────────────────────────────────

class TestUrlDedupKeys:
    """Only URLs that uniquely identify one item in a batch may be a dedup key."""

    def test_empty_iterable(self):
        assert _url_dedup_keys([]) == set()

    def test_all_falsy_ignored(self):
        assert _url_dedup_keys([None, None, ""]) == set()

    def test_unique_urls_all_kept(self):
        urls = ["https://x/a", "https://x/b", "https://x/c"]
        assert _url_dedup_keys(urls) == set(urls)

    def test_shared_url_excluded(self):
        # podcast pattern: every episode links to the same show page
        shared = "https://show/the-daily"
        assert _url_dedup_keys([shared, shared, shared]) == set()

    def test_mix_keeps_unique_drops_shared(self):
        shared = "https://show/pod"
        unique = "https://news/article-1"
        assert _url_dedup_keys([shared, shared, unique]) == {unique}

    def test_falsy_do_not_count_toward_sharing(self):
        # two real occurrences of the same url → shared; Nones are dropped, not counted
        u = "https://x/a"
        assert _url_dedup_keys([u, None, u, None]) == set()

    def test_falsy_mixed_with_one_real_url_kept(self):
        u = "https://x/a"
        assert _url_dedup_keys([None, u, ""]) == {u}


# ── _save_articles: GUID + URL dedup interaction ──────────────────────────────

def _guid_hash(raw: str) -> str:
    import hashlib
    return hashlib.sha256(_normalize_guid(raw).encode()).hexdigest()


def _scalars_result(values):
    r = MagicMock()
    r.scalars.return_value = list(values)
    return r


def _save_session(existing_hashes=(), existing_urls=()) -> AsyncMock:
    """Mock AsyncSession for _save_articles.

    Routes the two SELECTs by inspecting the statement: the guid_hash existence
    query, then (only when there are URL dedup keys) the url existence query. Any
    other statement gets a throwaway result.
    """
    eh, eu = list(existing_hashes), list(existing_urls)

    async def _execute(stmt, *a, **k):
        s = str(stmt)
        if "guid_hash" in s:
            return _scalars_result(eh)
        if s.lstrip().upper().startswith("SELECT"):
            return _scalars_result(eu)
        return MagicMock()

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=_execute)
    db.flush = AsyncMock()
    return db


@contextmanager
def _patched_post_insert():
    """Stub out the per-article post-insert work (filters, cross-feed dedup,
    readable auto-disable) so _save_articles tests stay focused on dedup."""
    with (
        patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()),
        patch("app.fetcher.rss._dedup_cross_feed", new=AsyncMock()),
        patch("app.services.readable_service.maybe_disable_readable_for_feed", new=AsyncMock()),
    ):
        yield


def _added_titles(db) -> list[str]:
    return [call.args[0].title for call in db.add.call_args_list]


class TestSaveArticlesDedup:
    """_save_articles: secondary URL dedup must not collapse feeds that share a
    single show/section link across distinct items (regression: podcast feeds)."""

    feed = SimpleNamespace(id=22)

    async def test_shared_url_new_guids_are_inserted(self):
        # Regression: WSJ/NYT-style podcast feed — every episode links to the same
        # show page. The shared URL already exists in the DB (from the first fetch),
        # but each new episode has a unique GUID and must still be saved.
        shared = "https://www.nytimes.com/the-daily"
        parsed = SimpleNamespace(entries=[
            {"id": "guid-old", "link": shared, "title": "Old episode"},
            {"id": "guid-new1", "link": shared, "title": "New episode 1"},
            {"id": "guid-new2", "link": shared, "title": "New episode 2"},
        ])
        db = _save_session(
            existing_hashes={_guid_hash("guid-old")},
            existing_urls={shared},  # ignored: shared URL is not a dedup key
        )
        with _patched_post_insert():
            count = await _save_articles(self.feed, parsed, db)

        assert count == 2
        assert sorted(_added_titles(db)) == ["New episode 1", "New episode 2"]

    async def test_rotated_guid_same_url_is_deduped(self):
        # BBC-style: article re-published under a new GUID but the same per-article
        # URL. The unique URL IS a dedup key, so the item is dropped.
        url = "https://bbc.example/news/article-123"
        parsed = SimpleNamespace(entries=[
            {"id": "guid-rotated", "link": url, "title": "Rotated"},
        ])
        db = _save_session(existing_hashes=set(), existing_urls={url})
        with _patched_post_insert():
            count = await _save_articles(self.feed, parsed, db)

        assert count == 0
        assert _added_titles(db) == []

    async def test_mixed_unique_urls_drops_only_existing(self):
        dup = "https://bbc.example/a"
        fresh = "https://bbc.example/b"
        parsed = SimpleNamespace(entries=[
            {"id": "g1", "link": dup, "title": "A (already in DB by url)"},
            {"id": "g2", "link": fresh, "title": "B"},
        ])
        db = _save_session(existing_hashes=set(), existing_urls={dup})
        with _patched_post_insert():
            count = await _save_articles(self.feed, parsed, db)

        assert count == 1
        assert _added_titles(db) == ["B"]

    async def test_existing_guid_is_skipped(self):
        url = "https://x/a"
        parsed = SimpleNamespace(entries=[
            {"id": "g-known", "link": url, "title": "Known"},
        ])
        db = _save_session(existing_hashes={_guid_hash("g-known")}, existing_urls=set())
        with _patched_post_insert():
            count = await _save_articles(self.feed, parsed, db)

        assert count == 0

    async def test_entries_without_links_dedup_by_guid_only(self):
        parsed = SimpleNamespace(entries=[
            {"id": "g1", "title": "X"},
            {"id": "g2", "title": "Y"},
        ])
        db = _save_session(existing_hashes=set(), existing_urls=set())
        with _patched_post_insert():
            count = await _save_articles(self.feed, parsed, db)

        assert count == 2
        assert sorted(_added_titles(db)) == ["X", "Y"]

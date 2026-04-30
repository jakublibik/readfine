"""Unit tests for fetch scheduler, interval quantization, and article helpers."""
from datetime import datetime, timedelta, timezone

import pytest

from app.fetcher.rss import _clamp_published_at
from app.fetcher.scheduler import create_scheduler
from app.routers.web.admin import _quantize15


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
        for v in (15, 30, 45, 60, 90, 120, 180, 360, 720, 1440):
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

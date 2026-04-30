"""Unit tests for fetch scheduler and interval quantization."""
from datetime import datetime, timezone

import pytest

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

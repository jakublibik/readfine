"""Unit tests for the adaptive fetch-interval derivation (app.fetcher.interval)."""
from datetime import datetime, timedelta, timezone

from app.fetcher.interval import (
    AUTO_FLOOR,
    FACTOR,
    WINDOW_MIN,
    auto_interval_min,
    derive_interval_min,
    quantize15,
)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
# A feed old enough to have a full window of history.
OLD = NOW - timedelta(days=30)


def _derive(count, created_at=OLD):
    return derive_interval_min(created_at=created_at, count=count, now=NOW)


class TestQuantize15:
    def test_rounds_to_nearest_15(self):
        assert quantize15(47) == 45
        assert quantize15(53) == 60

    def test_floored_at_15_never_zero(self):
        assert quantize15(0) == 15
        assert quantize15(7) == 15

    def test_no_upper_clamp(self):
        # Unlike admin._quantize15 (caps at 1440) — derived is stored uncapped.
        assert quantize15(7560) == 7560


class TestDeriveIntervalMin:
    def test_new_feed_returns_none(self):
        # Younger than the window → not enough history → caller uses the default.
        young = NOW - timedelta(days=2)
        assert derive_interval_min(created_at=young, count=500, now=NOW) is None

    def test_zero_and_one_collapse_to_same_value(self):
        # count==0 (quiet feed) must not divide by zero and must match count==1.
        expected = quantize15(WINDOW_MIN * FACTOR)  # window / 1 * factor
        assert _derive(0) == expected
        assert _derive(1) == expected

    def test_high_volume_hits_auto_floor(self):
        # Hundreds of items/week → raw well below AUTO_FLOOR → floored.
        assert _derive(1000) == AUTO_FLOOR

    def test_output_is_uncapped(self):
        # A quiet feed's derived value exceeds any sane cap — the cap is applied
        # at read time, not here.
        assert _derive(0) > 1440

    def test_quantized_to_15(self):
        assert _derive(50) % 15 == 0

    def test_midrange_matches_formula(self):
        # 100 items in the 7-day window → ~1.68 h gap → *0.75 ≈ 75.6 min → quantize 75.
        assert _derive(100) == quantize15(WINDOW_MIN / 100 * FACTOR)


class TestAutoIntervalMin:
    """The shared Auto resolver: caps a genuinely derived value, but leaves the default
    fallback uncapped (only floored). Single source of truth for scheduler + UI hints."""

    ARGS = dict(default_interval_min=60, min_interval_min=15, max_interval_min=360)

    def test_derived_capped_to_max(self):
        assert auto_interval_min(5040, **self.ARGS) == 360

    def test_derived_floored_to_min(self):
        args = dict(default_interval_min=60, min_interval_min=45, max_interval_min=360)
        assert auto_interval_min(30, **args) == 45

    def test_derived_in_range_unchanged(self):
        assert auto_interval_min(90, **self.ARGS) == 90

    def test_no_derived_uses_default(self):
        assert auto_interval_min(None, **self.ARGS) == 60

    def test_no_derived_default_above_cap_not_capped(self):
        # L1: an admin default above the cap is honoured for feeds without history —
        # the cap only trims genuinely derived (quiet-feed) values.
        args = dict(default_interval_min=720, min_interval_min=15, max_interval_min=360)
        assert auto_interval_min(None, **args) == 720

    def test_no_derived_default_floored_to_min(self):
        args = dict(default_interval_min=10, min_interval_min=15, max_interval_min=360)
        assert auto_interval_min(None, **args) == 15

"""Tests for the general rate-limit-header layer: rate_limited_until() parsing and
the in-memory per-host cooldown."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.fetcher import host_throttle
from app.utils.url_validator import rate_limited_until

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _headers(**kw) -> httpx.Headers:
    return httpx.Headers({k.replace("_", "-"): str(v) for k, v in kw.items()})


class TestRateLimitedUntil:
    def test_reddit_case_remaining_zero_uses_reset(self):
        # Reddit sends remaining=0.0 + reset (seconds) and NO Retry-After.
        h = _headers(**{"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "59"})
        until = rate_limited_until(h, NOW)
        assert until == NOW + timedelta(seconds=59)

    def test_retry_after_takes_precedence(self):
        h = _headers(**{"retry-after": "120", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "5"})
        # Retry-After wins and is clamped to a 60s floor by parse_retry_after.
        assert rate_limited_until(h, NOW) == NOW + timedelta(seconds=120)

    def test_ietf_ratelimit_spelling(self):
        h = _headers(**{"ratelimit-remaining": "0", "ratelimit-reset": "30"})
        assert rate_limited_until(h, NOW) == NOW + timedelta(seconds=30)

    def test_legacy_x_rate_limit_spelling(self):
        h = _headers(**{"x-rate-limit-remaining": "0", "x-rate-limit-reset": "10"})
        assert rate_limited_until(h, NOW) == NOW + timedelta(seconds=10)

    def test_remaining_positive_returns_none(self):
        h = _headers(**{"x-ratelimit-remaining": "5", "x-ratelimit-reset": "59"})
        assert rate_limited_until(h, NOW) is None

    def test_no_headers_returns_none(self):
        assert rate_limited_until(_headers(), NOW) is None

    def test_remaining_zero_but_no_reset_returns_none(self):
        h = _headers(**{"x-ratelimit-remaining": "0"})
        assert rate_limited_until(h, NOW) is None

    def test_reset_as_epoch_timestamp(self):
        future_epoch = int((NOW + timedelta(seconds=45)).timestamp())
        h = _headers(**{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(future_epoch)})
        until = rate_limited_until(h, NOW)
        assert until is not None
        assert abs((until - (NOW + timedelta(seconds=45))).total_seconds()) < 1

    def test_reset_epoch_in_past_returns_none(self):
        past_epoch = int((NOW - timedelta(seconds=45)).timestamp())
        h = _headers(**{"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(past_epoch)})
        assert rate_limited_until(h, NOW) is None

    def test_delta_reset_clamped_to_max(self):
        h = _headers(**{"x-ratelimit-remaining": "0", "x-ratelimit-reset": "999999"})
        until = rate_limited_until(h, NOW)
        assert until == NOW + timedelta(hours=24)

    def test_garbage_values_return_none(self):
        h = _headers(**{"x-ratelimit-remaining": "n/a", "x-ratelimit-reset": "soon"})
        assert rate_limited_until(h, NOW) is None


class TestHostCooldown:
    def setup_method(self):
        host_throttle.clear()

    def test_note_and_blocked(self):
        until = NOW + timedelta(seconds=59)
        host_throttle.note_rate_limited("reddit.com", until)
        assert host_throttle.blocked_until("reddit.com", NOW) == until

    def test_note_keeps_later_expiry(self):
        host_throttle.note_rate_limited("reddit.com", NOW + timedelta(seconds=30))
        host_throttle.note_rate_limited("reddit.com", NOW + timedelta(seconds=90))
        host_throttle.note_rate_limited("reddit.com", NOW + timedelta(seconds=10))
        assert host_throttle.blocked_until("reddit.com", NOW) == NOW + timedelta(seconds=90)

    def test_note_none_is_noop(self):
        host_throttle.note_rate_limited("reddit.com", None)
        assert host_throttle.blocked_until("reddit.com", NOW) is None

    def test_expired_cooldown_dropped(self):
        host_throttle.note_rate_limited("reddit.com", NOW + timedelta(seconds=59))
        later = NOW + timedelta(seconds=60)
        assert host_throttle.blocked_until("reddit.com", later) is None
        # confirm the entry was removed, not just filtered
        assert "reddit.com" not in host_throttle._cooldown

    def test_unknown_host_returns_none(self):
        assert host_throttle.blocked_until("example.com", NOW) is None

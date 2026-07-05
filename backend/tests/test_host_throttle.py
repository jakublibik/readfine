"""Tests for the general rate-limit-header layer: rate_limited_until() parsing and
the in-memory per-host cooldown."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.fetcher import host_throttle
from app.fetcher.rss import cooldown_until
from app.utils.url_validator import format_retry_in, rate_limited_until

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


class TestBlockBreather:
    """The 403 breather gates the scheduler (include_block=True) only, never the
    default (manual-facing) lookup."""

    def setup_method(self):
        host_throttle.clear()

    def test_block_hidden_from_default_lookup(self):
        host_throttle.note_block("reddit.com", NOW + timedelta(seconds=60))
        assert host_throttle.blocked_until("reddit.com", NOW) is None

    def test_block_visible_with_include_block(self):
        until = NOW + timedelta(seconds=60)
        host_throttle.note_block("reddit.com", until)
        assert host_throttle.blocked_until("reddit.com", NOW, include_block=True) == until

    def test_rate_limit_gates_both_lookups(self):
        until = NOW + timedelta(seconds=30)
        host_throttle.note_rate_limited("reddit.com", until)
        assert host_throttle.blocked_until("reddit.com", NOW) == until
        assert host_throttle.blocked_until("reddit.com", NOW, include_block=True) == until

    def test_include_block_returns_later_of_the_two(self):
        host_throttle.note_rate_limited("reddit.com", NOW + timedelta(seconds=30))
        host_throttle.note_block("reddit.com", NOW + timedelta(seconds=90))
        assert host_throttle.blocked_until("reddit.com", NOW, include_block=True) == NOW + timedelta(seconds=90)
        # ...but the manual-facing lookup still only sees the 30s rate-limit cooldown
        assert host_throttle.blocked_until("reddit.com", NOW) == NOW + timedelta(seconds=30)

    def test_clear_drops_block_breather(self):
        host_throttle.note_block("reddit.com", NOW + timedelta(seconds=60))
        host_throttle.clear()
        assert host_throttle.blocked_until("reddit.com", NOW, include_block=True) is None


def _feed(feed_url="https://www.reddit.com/r/rss/.rss", retry_after_until=None):
    return SimpleNamespace(feed_url=feed_url, retry_after_until=retry_after_until)


class TestCooldownUntil:
    """cooldown_until() gates manual fetches on the per-feed retry_after_until (DB)
    and the per-host in-memory throttle, returning the later of the two."""

    def setup_method(self):
        host_throttle.clear()

    def test_no_cooldown_returns_none(self):
        assert cooldown_until(_feed(), NOW) is None

    def test_per_feed_retry_after(self):
        until = NOW + timedelta(minutes=5)
        assert cooldown_until(_feed(retry_after_until=until), NOW) == until

    def test_expired_per_feed_ignored(self):
        assert cooldown_until(_feed(retry_after_until=NOW - timedelta(seconds=1)), NOW) is None

    def test_ignores_403_block_breather(self):
        # A 403 anti-bot breather must not block a manual refresh (cooldown_until is
        # the manual-facing gate) — only real rate-limit cooldowns do.
        host_throttle.note_block(host_throttle.host_key("https://reddit.com/x"), NOW + timedelta(seconds=60))
        assert cooldown_until(_feed(), NOW) is None

    def test_host_throttle_from_sibling_feed(self):
        # A sibling feed's 429 armed the host cooldown; this feed has no retry_after_until.
        until = NOW + timedelta(seconds=90)
        host_throttle.note_rate_limited(host_throttle.host_key("https://reddit.com/r/x/.rss"), until)
        assert cooldown_until(_feed(), NOW) == until

    def test_returns_later_of_the_two(self):
        feed_until = NOW + timedelta(seconds=30)
        host_until = NOW + timedelta(seconds=120)
        host_throttle.note_rate_limited(host_throttle.host_key("https://www.reddit.com/x"), host_until)
        assert cooldown_until(_feed(retry_after_until=feed_until), NOW) == host_until
        # ...and the reverse ordering also picks the later one
        assert cooldown_until(_feed(retry_after_until=host_until + timedelta(seconds=10)), NOW) \
            == host_until + timedelta(seconds=10)


class TestFormatRetryIn:
    def test_seconds_under_90(self):
        assert format_retry_in(NOW + timedelta(seconds=59), NOW) == "59 sec"

    def test_minutes_at_or_above_90(self):
        assert format_retry_in(NOW + timedelta(seconds=120), NOW) == "about 2 min"

    def test_floor_of_one_second(self):
        # An already-elapsed cooldown never renders "0 sec".
        assert format_retry_in(NOW, NOW) == "1 sec"

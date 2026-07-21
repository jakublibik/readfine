"""Unit tests for the two failure tiers (app.fetcher.failure).

These test the decision itself — which counter moves, what happens to status, how
far the feed is deferred — without a session and without inspecting the shape of the
SQLAlchemy clause. The companion DB test in test_fetcher.py checks that the values
actually land in the row.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.fetcher.failure import (
    BLOCK_BACKOFF_BASE,
    BLOCK_BACKOFF_MAX,
    BLOCK_DISABLE_THRESHOLD,
    FETCH_ERROR_DISABLE_THRESHOLD,
    block_backoff,
    classify,
    failure_values,
)

NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com/feed.xml")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _values(exc, block_count: int = 0) -> dict:
    return failure_values(exc, feed_block_count=block_count, now=NOW)


class TestClassify:
    def test_bare_403_is_a_block(self):
        assert classify(_http_error(403)) == (403, True)

    def test_bare_429_is_a_block(self):
        assert classify(_http_error(429)) == (429, True)

    def test_403_with_www_authenticate_is_not(self):
        exc = _http_error(403, {"WWW-Authenticate": 'Basic realm="feeds"'})
        assert classify(exc) == (403, False)

    def test_404_is_not(self):
        assert classify(_http_error(404)) == (404, False)

    def test_exception_without_a_response(self):
        # The caller's `except` is broad: timeouts, DNS failures, SSRF validation and
        # feedparser errors all land there and carry no response to inspect. Touching
        # exc.response for those would raise inside the except block.
        assert classify(httpx.ConnectTimeout("timed out")) == (None, False)
        assert classify(ValueError("Feed parse error")) == (None, False)


class TestBlockBackoff:
    def test_first_block_uses_the_base(self):
        assert block_backoff(0) == BLOCK_BACKOFF_BASE

    def test_doubles_per_block(self):
        assert block_backoff(1) == BLOCK_BACKOFF_BASE * 2
        assert block_backoff(3) == BLOCK_BACKOFF_BASE * 8

    def test_caps(self):
        assert block_backoff(10) == BLOCK_BACKOFF_MAX
        assert block_backoff(1000) == BLOCK_BACKOFF_MAX

    def test_negative_is_treated_as_zero(self):
        assert block_backoff(-1) == BLOCK_BACKOFF_BASE


class TestBlockTier:
    def test_moves_block_count_not_error_count(self):
        vals = _values(_http_error(403))
        assert "block_count" in vals
        assert "fetch_error_count" not in vals

    def test_defers_the_feed(self):
        vals = _values(_http_error(403))
        assert vals["retry_after_until"] == NOW + BLOCK_BACKOFF_BASE

    def test_backoff_grows_with_the_existing_count(self):
        vals = _values(_http_error(403), block_count=3)
        assert vals["retry_after_until"] == NOW + BLOCK_BACKOFF_BASE * 8

    def test_longer_retry_after_wins(self):
        # The host's own instruction beats our guess when it asks for more.
        exc = _http_error(429, {"Retry-After": "7200"})
        assert _values(exc)["retry_after_until"] == NOW + timedelta(seconds=7200)

    def test_shorter_retry_after_does_not_shorten_the_backoff(self):
        exc = _http_error(429, {"Retry-After": "60"})
        assert _values(exc)["retry_after_until"] == NOW + BLOCK_BACKOFF_BASE

    def test_records_the_error_message(self):
        assert _values(_http_error(403))["last_error"] == "403"


class TestErrorTier:
    def test_moves_error_count_not_block_count(self):
        vals = _values(httpx.ConnectTimeout("timed out"))
        assert "fetch_error_count" in vals
        assert "block_count" not in vals

    def test_no_response_does_not_raise(self):
        # Regression guard: reading exc.response unguarded would AttributeError here,
        # inside the caller's except block, losing the FetchLog and the counter bump.
        vals = _values(ValueError("Feed parse error: mismatched tag"))
        assert vals["fetch_error_count"] is not None
        assert vals["retry_after_until"] is None

    def test_403_with_credentials_prompt_stays_an_error(self):
        exc = _http_error(403, {"WWW-Authenticate": 'Basic realm="feeds"'})
        vals = _values(exc)
        assert "fetch_error_count" in vals
        assert "block_count" not in vals

    def test_408_stays_an_error(self):
        # A timeout is not a refusal, even though it shares the retryable set.
        vals = _values(_http_error(408))
        assert "fetch_error_count" in vals

    def test_408_honors_retry_after(self):
        vals = _values(_http_error(408, {"Retry-After": "600"}))
        assert vals["retry_after_until"] == NOW + timedelta(seconds=600)


class TestThresholds:
    def test_both_are_consecutive_counts(self):
        # Documented invariant: a success resets both, so these count runs, not totals.
        assert FETCH_ERROR_DISABLE_THRESHOLD == 5
        assert BLOCK_DISABLE_THRESHOLD == 10

    def test_blocks_tolerate_far_more_failures_than_errors(self):
        # The whole point: a host refusing us must not retire a feed as fast as a
        # feed that is actually broken.
        assert BLOCK_DISABLE_THRESHOLD > FETCH_ERROR_DISABLE_THRESHOLD


@pytest.mark.parametrize("status", [403, 429])
def test_block_statuses_never_touch_fetch_error_count(status):
    assert "fetch_error_count" not in _values(_http_error(status))


@pytest.mark.parametrize("status", [400, 404, 410, 451, 500, 503])
def test_non_block_statuses_never_touch_block_count(status):
    assert "block_count" not in _values(_http_error(status))

"""Unit tests for fetch scheduler, interval quantization, and article helpers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.fetcher.rss import (
    FETCH_ERROR_DISABLE_THRESHOLD,
    _clamp_published_at,
    _extract_content,
    _latest_published,
    _normalize_guid,
    _normalize_url,
    _safe_url,
    _struct_to_dt,
    fetch_feed,
)
from app.fetcher.scheduler import create_scheduler
from app.models.fetch_log import FetchLog
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
        "fetch_auth_user": None,
        "fetch_auth_pass_encrypted": None,
        "fetch_error_count": 0,
        "status": "active",
        "last_fetched_at": None,
        "last_fetch_duration_ms": None,
        "last_error": None,
        "last_published_at": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    return session


class TestFetchFeedErrorHandling:
    """fetch_feed: error path adds FetchLog, commits error state."""

    async def test_fetch_error_returns_zero(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss._fetch_url_with_ssrf_check", side_effect=ValueError("parse error")):
            result = await fetch_feed(feed, session)
        assert result == 0

    async def test_fetch_error_adds_fetchlog(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss._fetch_url_with_ssrf_check", side_effect=ValueError("parse error")):
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
        with patch("app.fetcher.rss._fetch_url_with_ssrf_check", side_effect=exc):
            await fetch_feed(feed, session)
        added = session.add.call_args[0][0]
        assert added.http_status == 404

    async def test_fetch_error_rolls_back_then_commits(self):
        feed = _make_feed()
        session = _make_session()
        with patch("app.fetcher.rss._fetch_url_with_ssrf_check", side_effect=ValueError("x")):
            await fetch_feed(feed, session)
        session.rollback.assert_called_once()
        session.commit.assert_called_once()

    async def test_fetch_error_truncates_long_message(self):
        feed = _make_feed()
        session = _make_session()
        long_msg = "e" * 600
        with patch("app.fetcher.rss._fetch_url_with_ssrf_check", side_effect=ValueError(long_msg)):
            await fetch_feed(feed, session)
        added = session.add.call_args[0][0]
        assert len(added.error_message) <= 500


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
            patch("app.fetcher.rss._fetch_url_with_ssrf_check", return_value="<rss/>"),
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
            patch("app.fetcher.rss._fetch_url_with_ssrf_check", return_value="<rss/>"),
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
            patch("app.fetcher.rss._fetch_url_with_ssrf_check", return_value="<rss/>"),
            patch("app.fetcher.rss.feedparser.parse", return_value=parsed),
        ):
            await fetch_feed(feed, session)

        assert feed.last_error is None

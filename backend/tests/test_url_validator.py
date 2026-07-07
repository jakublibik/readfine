"""Unit tests for SSRF-protection URL validator."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from unittest.mock import patch

from app.utils.url_validator import (
    RETRYABLE_HTTP_STATUSES,
    TRANSIENT_HTTP_STATUSES,
    fetch_url_conditional,
    parse_retry_after,
    redact_url,
    spacing_from_headers,
    validate_feed_url,
)

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestSpacingFromHeaders:
    def _h(self, **kw):
        return httpx.Headers({k.replace("_", "-"): str(v) for k, v in kw.items()})

    def test_reset_over_remaining(self):
        # 10 calls left over a 60s window → 6s apart.
        assert spacing_from_headers(self._h(ratelimit_remaining=10, ratelimit_reset=60), _NOW) == 6.0

    def test_single_call_window_equals_reset(self):
        assert spacing_from_headers(self._h(ratelimit_remaining=1, ratelimit_reset=60), _NOW) == 60.0

    def test_exhausted_budget_floors_remaining_to_one(self):
        # remaining=0 → spread the full reset window rather than dividing by zero.
        assert spacing_from_headers(self._h(ratelimit_remaining=0, ratelimit_reset=30), _NOW) == 30.0

    def test_legacy_x_spelling(self):
        h = self._h(**{"x-ratelimit-remaining": 4, "x-ratelimit-reset": 40})
        assert spacing_from_headers(h, _NOW) == 10.0

    def test_reset_as_epoch(self):
        epoch = int(_NOW.timestamp()) + 120
        h = self._h(ratelimit_remaining=2, ratelimit_reset=epoch)
        assert spacing_from_headers(h, _NOW) == 60.0

    def test_missing_headers_returns_none(self):
        assert spacing_from_headers(self._h(), _NOW) is None
        assert spacing_from_headers(self._h(ratelimit_remaining=5), _NOW) is None

    def test_expired_reset_returns_none(self):
        past = int(_NOW.timestamp()) - 10
        assert spacing_from_headers(self._h(ratelimit_remaining=5, ratelimit_reset=past), _NOW) is None


@contextmanager
def _mock_httpx_client(handler):
    """Patch the httpx.Client used by _resolve_response to use a MockTransport, so
    tests exercise the REAL redirect/304/error handling instead of mocking it out."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(*args, transport=transport, **kwargs)

    with patch("app.utils.url_validator.httpx.Client", factory):
        yield


class TestSchemeValidation:
    def test_https_allowed(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("1.2.3.4", 0))
        ]):
            validate_feed_url("https://example.com/feed.xml")  # should not raise

    def test_http_allowed(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("1.2.3.4", 0))
        ]):
            validate_feed_url("http://example.com/feed.xml")  # should not raise

    def test_ftp_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_feed_url("ftp://example.com/feed.xml")

    def test_file_rejected(self):
        with pytest.raises(ValueError, match="scheme"):
            validate_feed_url("file:///etc/passwd")

    def test_no_scheme_rejected(self):
        with pytest.raises(ValueError):
            validate_feed_url("example.com/feed.xml")


class TestSSRFProtection:
    def test_localhost_ipv4_rejected(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 0))
        ]):
            with pytest.raises(ValueError, match="disallowed"):
                validate_feed_url("http://localhost/feed")

    def test_loopback_ipv6_rejected(self):
        with patch("socket.getaddrinfo", return_value=[
            (10, 1, 6, "", ("::1", 0, 0, 0))
        ]):
            with pytest.raises(ValueError, match="disallowed"):
                validate_feed_url("http://localhost/feed")

    def test_private_rfc1918_10_rejected(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.1", 0))
        ]):
            with pytest.raises(ValueError, match="disallowed"):
                validate_feed_url("http://internal.corp/feed")

    def test_private_rfc1918_192_168_rejected(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("192.168.1.1", 0))
        ]):
            with pytest.raises(ValueError, match="disallowed"):
                validate_feed_url("http://router.local/feed")

    def test_link_local_rejected(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("169.254.0.1", 0))
        ]):
            with pytest.raises(ValueError, match="disallowed"):
                validate_feed_url("http://link-local.example/feed")

    def test_public_ip_allowed(self):
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0))
        ]):
            validate_feed_url("http://example.com/feed")  # should not raise


class TestHostnameValidation:
    def test_no_hostname_rejected(self):
        with pytest.raises(ValueError, match="hostname"):
            validate_feed_url("http:///path")

    def test_dns_failure_rejected(self):
        import socket
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")):
            with pytest.raises(ValueError, match="resolve"):
                validate_feed_url("http://nonexistent.invalid/feed")


class TestRedactUrl:
    def test_strips_query_string(self):
        assert redact_url("https://example.com/feed?api_key=secret123") == (
            "https://example.com/feed?<redacted>"
        )

    def test_strips_userinfo_credentials(self):
        # user:pass@host must not survive into logs
        out = redact_url("https://user:p4ss@example.com/feed.xml")
        assert "p4ss" not in out
        assert "user" not in out
        assert out == "https://example.com/feed.xml"

    def test_preserves_host_port_and_path(self):
        assert redact_url("http://example.com:8080/a/b/c") == "http://example.com:8080/a/b/c"

    def test_plain_url_without_query_unchanged(self):
        assert redact_url("https://example.com/feed.xml") == "https://example.com/feed.xml"

    def test_non_url_returned_as_is(self):
        assert redact_url("not a url") == "not a url"

    def test_strips_both_userinfo_and_query(self):
        out = redact_url("https://user:secret@example.com/feed?token=abc")
        assert "secret" not in out
        assert "token=abc" not in out
        assert out == "https://example.com/feed?<redacted>"


_NOW = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class TestParseRetryAfter:
    def test_delta_seconds(self):
        # well within bounds → exact delay
        assert parse_retry_after("600", _NOW) == _NOW + timedelta(seconds=600)

    def test_http_date(self):
        target = _NOW + timedelta(hours=2)
        header = target.strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(header, _NOW) == target

    def test_none_returns_none(self):
        assert parse_retry_after(None, _NOW) is None

    def test_blank_returns_none(self):
        assert parse_retry_after("   ", _NOW) is None

    def test_garbage_returns_none(self):
        assert parse_retry_after("soon-ish", _NOW) is None

    def test_floor_clamped_to_60s(self):
        # server asks 5 s, we never retry sooner than 60 s
        assert parse_retry_after("5", _NOW) == _NOW + timedelta(seconds=60)

    def test_ceiling_clamped_to_24h(self):
        assert parse_retry_after("999999", _NOW) == _NOW + timedelta(hours=24)

    def test_zero_seconds_returns_none(self):
        # delta-seconds of 0 is not a positive delay
        assert parse_retry_after("0", _NOW) is None

    def test_past_http_date_returns_none(self):
        past = (_NOW - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        assert parse_retry_after(past, _NOW) is None


class TestFetchUrlConditional:
    """fetch_url_conditional: validator injection, 304 passthrough, validator extraction."""

    def test_validators_sent_as_conditional_headers(self):
        with patch("app.utils.url_validator._resolve_response",
                   return_value=httpx.Response(304)) as mock_resolve:
            fetch_url_conditional(
                "https://example.com/feed.xml",
                etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
            )
        headers = mock_resolve.call_args[0][3]
        assert headers["If-None-Match"] == '"abc"'
        assert headers["If-Modified-Since"] == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_no_conditional_headers_without_validators(self):
        with patch("app.utils.url_validator._resolve_response",
                   return_value=httpx.Response(200)) as mock_resolve:
            fetch_url_conditional("https://example.com/feed.xml", headers={"User-Agent": "x"})
        headers = mock_resolve.call_args[0][3]
        assert "If-None-Match" not in headers
        assert "If-Modified-Since" not in headers
        assert headers["User-Agent"] == "x"

    def test_304_passthrough(self):
        with patch("app.utils.url_validator._resolve_response",
                   return_value=httpx.Response(304)):
            result = fetch_url_conditional("https://example.com/feed.xml", etag='"abc"')
        assert result.status_code == 304
        assert result.text == ""

    def test_200_extracts_returned_validators(self):
        resp = httpx.Response(
            200, text="<rss/>",
            headers={"ETag": '"new"', "Last-Modified": "Wed, 03 Jan 2024 00:00:00 GMT"},
        )
        with patch("app.utils.url_validator._resolve_response", return_value=resp):
            result = fetch_url_conditional("https://example.com/feed.xml")
        assert result.status_code == 200
        assert result.text == "<rss/>"
        assert result.etag == '"new"'
        assert result.last_modified == "Wed, 03 Jan 2024 00:00:00 GMT"

    def test_long_validators_truncated_to_255(self):
        resp = httpx.Response(200, headers={"ETag": "x" * 400})
        with patch("app.utils.url_validator._resolve_response", return_value=resp):
            result = fetch_url_conditional("https://example.com/feed.xml")
        assert len(result.etag) == 255


class TestResolveResponseIntegration:
    """End-to-end via a real httpx MockTransport — catches 304/redirect classification
    bugs that mocking _resolve_response would hide."""

    def test_304_returned_not_raised(self):
        # Regression: httpx classifies 304 as a redirect status; it must NOT be
        # followed as a redirect nor raised by raise_for_status.
        def handler(request):
            return httpx.Response(304)
        with _mock_httpx_client(handler):
            result = fetch_url_conditional("https://example.com/feed.xml", etag='"abc"')
        assert result.status_code == 304
        assert result.text == ""

    def test_200_returns_body_and_validators(self):
        def handler(request):
            return httpx.Response(
                200, text="<rss/>",
                headers={"ETag": '"v2"', "Last-Modified": "Wed, 03 Jan 2024 00:00:00 GMT"},
            )
        with _mock_httpx_client(handler):
            result = fetch_url_conditional("https://example.com/feed.xml")
        assert result.status_code == 200
        assert result.text == "<rss/>"
        assert result.etag == '"v2"'

    def test_429_raises_status_error(self):
        def handler(request):
            return httpx.Response(429, headers={"Retry-After": "60"})
        with _mock_httpx_client(handler):
            with pytest.raises(httpx.HTTPStatusError):
                fetch_url_conditional("https://example.com/feed.xml")

    def test_redirect_is_followed_with_ssrf_revalidation(self):
        def handler(request):
            if request.url.path == "/feed.xml":
                return httpx.Response(301, headers={"Location": "https://example.com/final.xml"})
            return httpx.Response(200, text="<rss/>ok")
        with _mock_httpx_client(handler):
            with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
                result = fetch_url_conditional("https://example.com/feed.xml")
        assert result.status_code == 200
        assert "ok" in result.text


class TestTransientHttpStatuses:
    def test_429_and_408_are_transient(self):
        assert 429 in TRANSIENT_HTTP_STATUSES
        assert 408 in TRANSIENT_HTTP_STATUSES

    def test_permanent_statuses_excluded(self):
        # 403 is not a rate-limit/timeout status → not "transient" for header handling
        for status in (400, 401, 403, 404, 410, 500, 503):
            assert status not in TRANSIENT_HTTP_STATUSES


class TestRetryableHttpStatuses:
    def test_403_408_429_are_retryable(self):
        # 403 backs off through the error tier instead of disabling on first hit
        assert 403 in RETRYABLE_HTTP_STATUSES
        assert 408 in RETRYABLE_HTTP_STATUSES
        assert 429 in RETRYABLE_HTTP_STATUSES

    def test_transient_is_a_subset(self):
        assert TRANSIENT_HTTP_STATUSES <= RETRYABLE_HTTP_STATUSES

    def test_permanent_4xx_still_excluded(self):
        for status in (400, 401, 404, 410):
            assert status not in RETRYABLE_HTTP_STATUSES

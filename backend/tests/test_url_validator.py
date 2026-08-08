"""Unit tests for SSRF-protection URL validator."""
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from unittest.mock import patch

from tests.conftest import mock_httpx_client as _mock_httpx_client
from app.utils.url_validator import (
    RETRYABLE_HTTP_STATUSES,
    TRANSIENT_HTTP_STATUSES,
    _pin_connection,
    fetch_url_conditional,
    fetch_url_page,
    fetch_url_with_ssrf_check,
    is_bot_block,
    parse_retry_after,
    rate_limited_until,
    redact_url,
    redact_url_for_display,
    spacing_from_headers,
    validate_feed_url,
)

_NOW = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
# Stands in for the third element of a _resolve_response result (where the redirect
# chain ended) in tests that mock the fetch out and only care about the response.
_URL = "https://example.com/feed.xml"


class TestSpacingFromHeaders:
    def _h(self, **kw):
        return httpx.Headers({k.replace("_", "-"): str(v) for k, v in kw.items()})

    def test_reset_over_remaining(self):
        # 10 calls left over a 60s window → 6s apart.
        assert spacing_from_headers(self._h(ratelimit_remaining=10, ratelimit_reset=60), _NOW) == 6.0

    def test_single_call_window_equals_reset(self):
        assert spacing_from_headers(self._h(ratelimit_remaining=1, ratelimit_reset=60), _NOW) == 60.0

    def test_exhausted_budget_yields_no_spacing(self):
        # remaining=0 → `reset` is the phase left in the current window, not a period.
        # It depends on when we asked, not on any rate: Reddit answers every request
        # with remaining=0 and a reset that counts down to the next wall-clock minute,
        # so consecutive requests 84s apart report 59, then 35, then 11. Feeding that
        # to the monotonic ratchet in host_throttle "learned" the window length from
        # sampling noise. The deadline reading belongs to rate_limited_until instead.
        assert spacing_from_headers(self._h(ratelimit_remaining=0, ratelimit_reset=30), _NOW) is None

    def test_exhausted_budget_still_yields_a_cooldown(self):
        # The same headers remain a valid "not before now + reset" deadline.
        h = self._h(ratelimit_remaining=0, ratelimit_reset=30)
        assert rate_limited_until(h, _NOW) == _NOW + timedelta(seconds=30)

    def test_budget_with_room_still_yields_a_rate(self):
        # The fix must not silence the case the function exists for: a live budget
        # with requests left in it really is reset/remaining.
        assert spacing_from_headers(self._h(ratelimit_remaining=4, ratelimit_reset=60), _NOW) == 15.0

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


class TestIsBotBlock:
    """403/429 without WWW-Authenticate = the host refusing automation."""

    def _h(self, **kw):
        return httpx.Headers({k.replace("_", "-"): str(v) for k, v in kw.items()})

    def test_bare_403_is_a_block(self):
        assert is_bot_block(403, self._h()) is True

    def test_bare_429_is_a_block(self):
        assert is_bot_block(429, self._h()) is True

    def test_403_with_www_authenticate_is_an_auth_failure(self):
        h = self._h(**{"www-authenticate": 'Basic realm="feeds"'})
        assert is_bot_block(403, h) is False

    def test_403_with_ratelimit_headers_is_still_a_block(self):
        # Reddit sends both shapes for the same condition; a throttle is not a feed fault.
        h = self._h(**{"x-ratelimit-remaining": 0, "x-ratelimit-reset": 45})
        assert is_bot_block(403, h) is True

    def test_404_is_not_a_block(self):
        assert is_bot_block(404, self._h()) is False

    def test_500_is_not_a_block(self):
        assert is_bot_block(500, self._h()) is False

    def test_no_status_is_not_a_block(self):
        # Timeouts and DNS failures reach the caller with no status at all.
        assert is_bot_block(None, self._h()) is False


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


class TestRedactUrlForDisplay:
    def test_strips_userinfo_credentials(self):
        out = redact_url_for_display("https://user:p4ss@example.com/feed.xml")
        assert out == "https://example.com/feed.xml"

    def test_strips_userinfo_without_password(self):
        assert redact_url_for_display("https://user@example.com/feed") == (
            "https://example.com/feed"
        )

    def test_redacts_secret_query_value(self):
        assert redact_url_for_display("https://example.com/feed?api_key=secret123") == (
            "https://example.com/feed?api_key=<redacted>"
        )

    def test_keeps_harmless_query_params(self):
        # The admin feed list is read to tell rows apart; channel_id is what
        # distinguishes two YouTube feeds on the same host and path.
        url = "https://youtube.com/feeds/videos.xml?channel_id=UC123"
        assert redact_url_for_display(url) == url

    def test_redacts_only_the_secret_param(self):
        out = redact_url_for_display("https://example.com/f?channel_id=UC1&token=abc")
        assert out == "https://example.com/f?channel_id=UC1&token=<redacted>"

    def test_secret_key_matched_case_insensitively(self):
        out = redact_url_for_display("https://example.com/f?API_KEY=abc")
        assert "abc" not in out

    def test_plain_url_returned_unchanged(self):
        # Callers compare against the input to decide whether it is still linkable,
        # so an untouched URL has to come back byte for byte.
        url = "https://example.com:8080/a/b?q=hello%20world&page=2"
        assert redact_url_for_display(url) is url

    def test_uppercase_host_survives_untouched(self):
        url = "https://Example.COM/feed"
        assert redact_url_for_display(url) is url

    def test_non_url_returned_as_is(self):
        assert redact_url_for_display("not a url") == "not a url"

    def test_empty_secret_value_still_redacted(self):
        assert redact_url_for_display("https://example.com/f?token=") == (
            "https://example.com/f?token=<redacted>"
        )


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
                   return_value=(httpx.Response(304), None, _URL)) as mock_resolve:
            fetch_url_conditional(
                "https://example.com/feed.xml",
                etag='"abc"', last_modified="Mon, 01 Jan 2024 00:00:00 GMT",
            )
        headers = mock_resolve.call_args[0][3]
        assert headers["If-None-Match"] == '"abc"'
        assert headers["If-Modified-Since"] == "Mon, 01 Jan 2024 00:00:00 GMT"

    def test_no_conditional_headers_without_validators(self):
        with patch("app.utils.url_validator._resolve_response",
                   return_value=(httpx.Response(200), None, _URL)) as mock_resolve:
            fetch_url_conditional("https://example.com/feed.xml", headers={"User-Agent": "x"})
        headers = mock_resolve.call_args[0][3]
        assert "If-None-Match" not in headers
        assert "If-Modified-Since" not in headers
        assert headers["User-Agent"] == "x"

    def test_304_passthrough(self):
        with patch("app.utils.url_validator._resolve_response",
                   return_value=(httpx.Response(304), None, _URL)):
            result = fetch_url_conditional("https://example.com/feed.xml", etag='"abc"')
        assert result.status_code == 304
        assert result.text == ""

    def test_200_extracts_returned_validators(self):
        resp = httpx.Response(
            200, text="<rss/>",
            headers={"ETag": '"new"', "Last-Modified": "Wed, 03 Jan 2024 00:00:00 GMT"},
        )
        with patch("app.utils.url_validator._resolve_response", return_value=(resp, None, _URL)):
            result = fetch_url_conditional("https://example.com/feed.xml")
        assert result.status_code == 200
        assert result.text == "<rss/>"
        assert result.etag == '"new"'
        assert result.last_modified == "Wed, 03 Jan 2024 00:00:00 GMT"

    def test_long_validators_truncated_to_255(self):
        resp = httpx.Response(200, headers={"ETag": "x" * 400})
        with patch("app.utils.url_validator._resolve_response", return_value=(resp, None, _URL)):
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


class TestPermanentRedirectTarget:
    """The address a stored feed URL may safely be rewritten to.

    End-to-end through the real redirect loop: the interesting behaviour is which
    hop the permanent prefix stops at, which a unit test of the guard alone misses.
    """

    _PUBLIC_IP = [(2, 1, 6, "", ("93.184.216.34", 0))]

    def _chain(self, start, *hops):
        """Fetch *start* through *hops* ((status, location), …), return permanent_url.

        Hops are served in order and the last request answers 200, so a chain may
        revisit the same path (http → https on one URL) without looping.
        """
        remaining = list(hops)

        def handler(request):
            if not remaining:
                return httpx.Response(200, text="<rss/>")
            status, location = remaining.pop(0)
            return httpx.Response(status, headers={"Location": location})

        with _mock_httpx_client(handler):
            with patch("socket.getaddrinfo", return_value=self._PUBLIC_IP):
                return fetch_url_page(start).permanent_url

    def test_all_permanent_chain_yields_final_url(self):
        # The vice.com shape: http → 301 https → 301 trailing slash → 200.
        assert self._chain(
            "http://example.com/feed",
            (301, "https://example.com/feed"),
            (301, "https://example.com/feed/"),
        ) == "https://example.com/feed/"

    def test_permanent_prefix_survives_a_later_temporary_hop(self):
        # The 301 said "/feed moved to /moved" for good; whatever the 302 does after
        # that cannot unsay it, so the permanent prefix is still worth storing.
        assert self._chain(
            "https://example.com/feed",
            (301, "https://example.com/moved"),
            (302, "https://example.com/temporary"),
        ) == "https://example.com/moved"

    def test_temporary_first_hop_yields_nothing(self):
        assert self._chain(
            "https://example.com/feed",
            (302, "https://example.com/elsewhere"),
            (301, "https://example.com/final"),
        ) is None

    def test_no_redirect_yields_nothing(self):
        assert self._chain("https://example.com/feed") is None

    def test_userinfo_in_original_blocks_adoption(self):
        # A Location header never carries credentials, so adopting the target would
        # silently drop them and turn every later fetch into a 401.
        assert self._chain(
            "https://user:pass@example.com/feed",
            (301, "https://example.com/moved"),
        ) is None

    def test_userinfo_in_target_blocks_adoption(self):
        # An honest Location carries no credentials, but a hostile feed host can put
        # anything in one. Adopting them would store credentials on a row the user
        # never marked private, so every subscriber would start sending them, and the
        # feed URL links would render a host that is not the one being fetched.
        assert self._chain(
            "https://example.com/feed",
            (301, "https://trusted.example@evil.example/feed"),
        ) is None

    def test_query_gained_blocks_adoption(self):
        # A session/CDN parameter would be baked in and expire days later.
        assert self._chain(
            "https://example.com/feed",
            (301, "https://example.com/feed?session=abc"),
        ) is None

    def test_query_lost_blocks_adoption(self):
        # Feeds authenticated by a query token must not lose it.
        assert self._chain(
            "https://example.com/feed?api_key=secret",
            (301, "https://example.com/feed"),
        ) is None

    def test_unchanged_query_is_adopted(self):
        assert self._chain(
            "https://example.com/feed?format=rss",
            (301, "https://www.example.com/feed?format=rss"),
        ) == "https://www.example.com/feed?format=rss"

    def test_https_to_http_downgrade_blocks_adoption(self):
        assert self._chain(
            "https://example.com/feed",
            (301, "http://example.com/feed"),
        ) is None

    def test_conditional_fetch_reports_permanent_url_too(self):
        served = []

        def handler(request):
            served.append(request)
            if len(served) == 1:
                return httpx.Response(301, headers={"Location": "https://example.com/moved"})
            return httpx.Response(304)

        with _mock_httpx_client(handler):
            with patch("socket.getaddrinfo", return_value=self._PUBLIC_IP):
                result = fetch_url_conditional("https://example.com/feed", etag='"abc"')
        assert result.status_code == 304
        assert result.permanent_url == "https://example.com/moved"


class TestFinalUrl:
    """fetch_url_page reports where the chain ended, separately from what may be stored.

    A temporary redirect must not be adopted as the feed's address, but it does decide
    which page the body came from — readable extraction reads relative links and the
    "did this send us back?" check off that address.
    """

    _PUBLIC_IP = [(2, 1, 6, "", ("93.184.216.34", 0))]

    def _fetch(self, start, *hops):
        remaining = list(hops)

        def handler(request):
            if not remaining:
                return httpx.Response(200, text="<html/>")
            status, location = remaining.pop(0)
            return httpx.Response(status, headers={"Location": location})

        with _mock_httpx_client(handler):
            with patch("socket.getaddrinfo", return_value=self._PUBLIC_IP):
                return fetch_url_page(start)

    def test_temporary_redirect_sets_final_url_but_not_permanent_url(self):
        page = self._fetch(
            "https://example.com/a", (302, "https://example.com/consent?back=/a")
        )
        assert page.final_url == "https://example.com/consent?back=/a"
        assert page.permanent_url is None

    def test_without_redirects_final_url_is_the_requested_one(self):
        page = self._fetch("https://example.com/a")
        assert page.final_url == "https://example.com/a"

    def test_final_url_is_the_last_hop_not_the_permanent_prefix(self):
        page = self._fetch(
            "https://example.com/a",
            (301, "https://example.com/moved"),
            (302, "https://cdn.example/moved"),
        )
        assert page.final_url == "https://cdn.example/moved"
        assert page.permanent_url == "https://example.com/moved"

    def test_relative_location_is_resolved(self):
        page = self._fetch("https://example.com/a/b", (302, "/c"))
        assert page.final_url == "https://example.com/c"


class TestAuthStaysOnItsOrigin:
    """Credentials are sent to the host they were given for, and to no other.

    Set on the httpx client they would ride the whole redirect chain, so a feed host
    answering one 302 could hand its neighbour the subscriber's Basic auth header.
    """

    _PUBLIC_IP = [(2, 1, 6, "", ("93.184.216.34", 0))]

    def _authorization_headers(self, start, *hops, auth=("bob", "hunter2")):
        """Fetch *start* through *hops*, returning each hop's Authorization header."""
        remaining = list(hops)
        seen: list[str | None] = []

        def handler(request):
            seen.append(request.headers.get("authorization"))
            if not remaining:
                return httpx.Response(200, text="<rss/>")
            status, location = remaining.pop(0)
            return httpx.Response(status, headers={"Location": location})

        with _mock_httpx_client(handler):
            with patch("socket.getaddrinfo", return_value=self._PUBLIC_IP):
                fetch_url_page(start, auth=auth)
        return seen

    def test_sent_on_the_first_request(self):
        assert self._authorization_headers("https://example.com/feed")[0] is not None

    def test_sent_across_a_same_origin_redirect(self):
        seen = self._authorization_headers(
            "https://example.com/feed", (301, "https://example.com/feed/")
        )
        assert seen[0] is not None and seen[1] == seen[0]

    def test_default_port_spelled_out_is_still_the_same_origin(self):
        seen = self._authorization_headers(
            "https://example.com/feed", (301, "https://example.com:443/feed/")
        )
        assert seen[1] == seen[0]

    def test_withheld_after_a_hop_to_another_host(self):
        seen = self._authorization_headers(
            "https://example.com/feed", (302, "https://evil.example/feed")
        )
        assert seen[0] is not None
        assert seen[1] is None

    def test_kept_across_an_http_to_https_upgrade_on_the_same_host(self):
        # The shape every feed stored under an http:// address takes on each fetch.
        # The host is upgrading its own address, not handing us to someone else.
        seen = self._authorization_headers(
            "http://example.com/feed", (301, "https://example.com/feed")
        )
        assert seen[1] == seen[0] is not None

    def test_upgrade_exception_does_not_extend_to_another_host(self):
        seen = self._authorization_headers(
            "http://example.com/feed", (301, "https://evil.example/feed")
        )
        assert seen[1] is None

    def test_withheld_after_an_https_to_http_downgrade(self):
        seen = self._authorization_headers(
            "https://example.com/feed", (302, "http://example.com/feed")
        )
        assert seen[1] is None

    def test_withheld_after_a_port_change(self):
        seen = self._authorization_headers(
            "https://example.com/feed", (302, "https://example.com:8443/feed")
        )
        assert seen[1] is None

    def test_not_regained_by_returning_to_the_original_host(self):
        # The header has already been offered to evil.example by the time the chain
        # comes back, but there is no reason to keep punishing the real host for it:
        # the hop is on the origin the credentials were given for, so it gets them.
        seen = self._authorization_headers(
            "https://example.com/feed",
            (302, "https://evil.example/hop"),
            (302, "https://example.com/feed2"),
        )
        assert [h is None for h in seen] == [False, True, False]

    def test_userinfo_in_the_url_still_authenticates_without_explicit_auth(self):
        # _pin_connection keeps userinfo in the connect URL and httpx builds the
        # header from it. Passing auth=None per request must not disable that.
        seen = self._authorization_headers(
            "https://bob:hunter2@example.com/feed", auth=None
        )
        assert seen[0] is not None


class TestPinConnection:
    """_pin_connection: rewrite to the validated IP while preserving Host / SNI."""

    def test_https_rewrites_to_ip_with_host_and_sni(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            connect_url, headers, extensions = _pin_connection("https://example.com/feed.xml")
        assert connect_url == "https://93.184.216.34/feed.xml"
        assert headers["Host"] == "example.com"
        assert extensions["sni_hostname"] == "example.com"

    def test_http_sets_no_sni(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            connect_url, headers, extensions = _pin_connection("http://example.com/feed.xml")
        assert connect_url == "http://93.184.216.34/feed.xml"
        assert "sni_hostname" not in extensions

    def test_port_and_query_preserved(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            connect_url, headers, _ = _pin_connection("https://example.com:8443/f?a=1")
        assert connect_url == "https://93.184.216.34:8443/f?a=1"
        assert headers["Host"] == "example.com:8443"

    def test_userinfo_preserved(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            connect_url, headers, _ = _pin_connection("https://user:pass@example.com/f")
        assert connect_url == "https://user:pass@93.184.216.34/f"
        assert headers["Host"] == "example.com"  # Host carries no credentials

    def test_ipv6_is_bracketed(self):
        with patch("socket.getaddrinfo", return_value=[(10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 0, 0, 0))]):
            connect_url, _, _ = _pin_connection("https://example.com/f")
        assert connect_url == "https://[2606:2800:220:1:248:1893:25c8:1946]/f"

    def test_rejects_when_any_resolved_address_is_private(self):
        # A public + private mix (rebinding-style) is rejected outright, not
        # silently pinned to the public one.
        with patch("socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]):
            with pytest.raises(ValueError, match="disallowed address"):
                _pin_connection("https://example.com/f")


class TestDnsRebindingClosure:
    """The fetch connects to the IP validated at check time, not a re-resolved one."""

    def test_fetch_targets_pinned_ip_not_hostname(self):
        seen = {}

        def handler(request):
            seen["host"] = request.url.host
            seen["header"] = request.headers.get("host")
            return httpx.Response(200, text="ok")

        with _mock_httpx_client(handler):
            with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
                fetch_url_with_ssrf_check("https://example.com/feed.xml")
        # httpx is asked to connect to the validated IP; the hostname rides in Host.
        assert seen["host"] == "93.184.216.34"
        assert seen["header"] == "example.com"


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


class TestOutboundLogging:
    """The outbound request log is a diagnostic switch: silent unless asked for,
    and never able to break a fetch."""

    def _log(self, caplog, *, enabled, response=None, error=None):
        from app.utils.url_validator import log_outbound

        with patch("app.config.settings.log_outbound_requests", enabled):
            with caplog.at_level(logging.INFO, logger="app.utils.url_validator"):
                log_outbound("https://example.com/feed", response, 0.0, error=error)
        return caplog.text

    def test_silent_when_disabled(self, caplog):
        resp = httpx.Response(200, request=httpx.Request("GET", "https://example.com/feed"))
        assert "outbound" not in self._log(caplog, enabled=False, response=resp)

    def test_logs_status_and_rate_limit_headers(self, caplog):
        resp = httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0.0", "x-ratelimit-reset": "20"},
            request=httpx.Request("GET", "https://example.com/feed"),
        )
        text = self._log(caplog, enabled=True, response=resp)
        assert "host=example.com" in text
        assert "status=403" in text
        assert "rl_remaining=0.0" in text
        assert "rl_reset=20" in text

    def test_logs_transport_failures(self, caplog):
        text = self._log(caplog, enabled=True, response=None, error="ConnectTimeout")
        assert "status=ERR(ConnectTimeout)" in text

    def test_credentials_are_redacted(self, caplog):
        from app.utils.url_validator import log_outbound

        resp = httpx.Response(200, request=httpx.Request("GET", "https://example.com/feed"))
        with patch("app.config.settings.log_outbound_requests", True):
            with caplog.at_level(logging.INFO, logger="app.utils.url_validator"):
                log_outbound("https://user:secret@example.com/feed?token=abc", resp, 0.0)
        assert "secret" not in caplog.text
        assert "token=abc" not in caplog.text

    def test_never_raises(self, caplog):
        # A malformed response object must not propagate out of the logger.
        assert self._log(caplog, enabled=True, response=object()) is not None


class TestProtocolErrorRetry:
    """A connection dying mid-request is retried once; other failures are not."""

    def _pin_localhost(self):
        return patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))])

    def test_goaway_is_retried_and_succeeds(self):
        attempts = []

        def handler(request):
            attempts.append(request.url)
            if len(attempts) == 1:
                raise httpx.RemoteProtocolError(
                    "<ConnectionTerminated error_code:0>", request=request
                )
            return httpx.Response(200, text="feed body")

        with _mock_httpx_client(handler), self._pin_localhost():
            body = fetch_url_with_ssrf_check("https://example.com/feed.xml")
        assert body == "feed body"
        assert len(attempts) == 2

    def test_second_protocol_error_gives_up(self):
        attempts = []

        def handler(request):
            attempts.append(request.url)
            raise httpx.RemoteProtocolError("Server disconnected", request=request)

        with _mock_httpx_client(handler), self._pin_localhost():
            with pytest.raises(httpx.RemoteProtocolError):
                fetch_url_with_ssrf_check("https://example.com/feed.xml")
        assert len(attempts) == 2  # one retry, then the error is the answer

    def test_timeout_is_not_retried(self):
        attempts = []

        def handler(request):
            attempts.append(request.url)
            raise httpx.ReadTimeout("timed out", request=request)

        with _mock_httpx_client(handler), self._pin_localhost():
            with pytest.raises(httpx.ReadTimeout):
                fetch_url_with_ssrf_check("https://example.com/feed.xml")
        assert len(attempts) == 1

    def test_retry_reuses_the_pinned_ip(self):
        hosts = []

        def handler(request):
            hosts.append((request.url.host, request.headers.get("host")))
            if len(hosts) == 1:
                raise httpx.RemoteProtocolError("GOAWAY", request=request)
            return httpx.Response(200, text="ok")

        with _mock_httpx_client(handler):
            with self._pin_localhost() as resolve:
                fetch_url_with_ssrf_check("https://example.com/feed.xml")
        # Same validated IP both times, and DNS was consulted only once.
        assert hosts == [("93.184.216.34", "example.com")] * 2
        assert resolve.call_count == 1

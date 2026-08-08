"""readable_service._fetch_html — the download half of readable extraction.

The interesting property here is that this path no longer resolves an address twice.
It used to validate the hostname and then hand the *hostname* to httpx, which resolved
it again at connect time; anything that answers DNS could return a public address to
the check and a private one to the connect. Save-by-URL made that window reachable on
demand and repeatedly, which is what a race needs, so the download now goes through the
same pinned fetch the feed fetcher uses.

The tests drive the real redirect loop through a MockTransport rather than mocking the
fetch out, so they fail if the pinning or the error mapping is bypassed.
"""
import httpx
from unittest.mock import patch

from app.services.readable_service import _fetch_html, _MAX_REDIRECTS, _TOO_LARGE_MSG
from tests.conftest import mock_httpx_client

_PUBLIC_IP = [(2, 1, 6, "", ("93.184.216.34", 0))]
_METADATA_IP = [(2, 1, 6, "", ("169.254.169.254", 0))]
_PRIVATE_IP = [(2, 1, 6, "", ("10.0.0.5", 0))]


def _ok(_request):
    return httpx.Response(200, text="<html><body><p>hello</p></body></html>")


class TestPinnedDownload:
    def test_connects_to_the_validated_ip_with_the_real_host(self):
        seen = {}

        def handler(request):
            seen["connect_host"] = request.url.host
            seen["host_header"] = request.headers.get("host")
            return _ok(request)

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            html, error, status, final_url = _fetch_html("https://example.com/a", None, None)

        assert error is None and status is None
        assert "hello" in html
        # The socket goes to the address we checked; the name rides in Host (and SNI).
        assert seen["connect_host"] == "93.184.216.34"
        assert seen["host_header"] == "example.com"
        assert final_url == "https://example.com/a"

    def test_basic_auth_is_sent_when_both_parts_are_present(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return _ok(request)

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            _fetch_html("https://example.com/a", "user", "pass")
        assert seen["auth"].startswith("Basic ")

    def test_no_auth_header_without_credentials(self):
        seen = {}

        def handler(request):
            seen["auth"] = request.headers.get("authorization")
            return _ok(request)

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            _fetch_html("https://example.com/a", "user", None)
        assert seen["auth"] is None


class TestBlockedAddresses:
    def test_metadata_address_is_refused_before_any_request(self):
        called = []

        def handler(request):
            called.append(request)
            return _ok(request)

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_METADATA_IP):
            html, error, status, final_url = _fetch_html("http://metadata.example/a", None, None)

        assert html is None and status is None and final_url is None
        assert "disallowed" in error
        assert not called

    def test_redirect_into_a_private_range_is_refused_and_named_as_such(self):
        # The address the user asked for is fine; the host's Location header is not.
        def handler(request):
            if request.url.host == "93.184.216.34":
                return httpx.Response(302, headers={"Location": "http://internal.example/admin"})
            return _ok(request)

        def resolve(hostname, *args, **kwargs):
            return _PUBLIC_IP if hostname == "example.com" else _PRIVATE_IP

        with mock_httpx_client(handler), patch("socket.getaddrinfo", side_effect=resolve):
            html, error, status, final_url = _fetch_html("https://example.com/a", None, None)

        assert html is None
        assert error.startswith("Redirect blocked:")
        assert "disallowed" in error

    def test_non_http_scheme_is_refused(self):
        html, error, _status, _final = _fetch_html("file:///etc/passwd", None, None)
        assert html is None
        assert "scheme" in error


class TestErrorMapping:
    """The failure shapes apply_readable_result and the 403 auto-disable rely on."""

    def test_http_error_reports_its_status(self):
        with mock_httpx_client(lambda r: httpx.Response(403)), \
             patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            html, error, status, _final = _fetch_html("https://example.com/a", None, None)
        assert html is None
        assert status == 403
        assert error.startswith("HTTP 403")

    def test_timeout_is_reported_without_a_status(self):
        def handler(request):
            raise httpx.ConnectTimeout("too slow", request=request)

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            html, error, status, _final = _fetch_html("https://example.com/a", None, None)
        assert html is None and status is None
        assert error.startswith("Timeout after")

    def test_oversized_page_is_reported_without_the_cap_s_own_size(self):
        # The reader cannot do anything about a server setting, so the message names
        # the problem and leaves the byte count to the log.
        from app.config import settings

        def handler(request):
            return httpx.Response(200, content=iter([b"x" * 5000]))

        with mock_httpx_client(handler), \
             patch("socket.getaddrinfo", return_value=_PUBLIC_IP), \
             patch.object(settings, "max_fetch_bytes", 1000):
            html, error, status, _final = _fetch_html("https://example.com/a", None, None)
        assert html is None and status is None
        assert error == _TOO_LARGE_MSG

    def test_redirect_loop_gives_up_with_the_readable_limit(self):
        def handler(request):
            return httpx.Response(302, headers={"Location": f"https://example.com/{request.url.path}x"})

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            html, error, _status, _final = _fetch_html("https://example.com/a", None, None)
        assert html is None
        assert error == f"Too many redirects (max {_MAX_REDIRECTS})"


class TestFinalUrl:
    """What extract_readable_with_title reads the article's real address off."""

    def test_redirect_chain_end_is_returned(self):
        hops = {
            "/a": (301, "https://example.com/b"),
            "/b": (302, "https://example.com/c"),
        }

        def handler(request):
            hop = hops.get(request.url.path)
            if hop:
                return httpx.Response(hop[0], headers={"Location": hop[1]})
            return _ok(request)

        with mock_httpx_client(handler), patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
            _html, error, _status, final_url = _fetch_html("https://example.com/a", None, None)
        assert error is None
        assert final_url == "https://example.com/c"

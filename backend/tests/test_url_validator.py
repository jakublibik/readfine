"""Unit tests for SSRF-protection URL validator."""
import pytest
from unittest.mock import patch

from app.utils.url_validator import redact_url, validate_feed_url


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

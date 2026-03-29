"""Unit tests for SSRF-protection URL validator."""
import pytest
from unittest.mock import patch

from app.utils.url_validator import validate_feed_url


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

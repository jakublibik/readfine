"""Unit tests for feed_detect: auto-detection of RSS/Atom feeds from HTML pages."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.feed_detect import _dedup, _youtube_feed_url, detect_feeds

# ── _youtube_feed_url ─────────────────────────────────────────────────────────

class TestYoutubeFeedUrl:
    def test_channel_url_converted(self):
        url = "https://www.youtube.com/channel/UCxxxxYYYzzzz1234567890"
        result = _youtube_feed_url(url)
        assert result == "https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxxYYYzzzz1234567890"

    def test_user_url_converted(self):
        url = "https://www.youtube.com/user/exampleuser"
        result = _youtube_feed_url(url)
        assert result == "https://www.youtube.com/feeds/videos.xml?user=exampleuser"

    def test_non_youtube_returns_none(self):
        assert _youtube_feed_url("https://example.com/channel/UCxxx") is None

    def test_youtube_handle_returns_none(self):
        # @handle needs channel ID from the page — not resolved statically
        assert _youtube_feed_url("https://www.youtube.com/@somehandle") is None

    def test_non_url_string_returns_none(self):
        assert _youtube_feed_url("not a url") is None


# ── _dedup ────────────────────────────────────────────────────────────────────

class TestDedup:
    def test_unique_urls_unchanged(self):
        feeds = [
            {"url": "https://a.com/feed", "title": "A"},
            {"url": "https://b.com/feed", "title": "B"},
        ]
        assert _dedup(feeds) == feeds

    def test_duplicate_url_removed(self):
        feeds = [
            {"url": "https://a.com/feed", "title": "First"},
            {"url": "https://a.com/feed", "title": "Second"},
        ]
        result = _dedup(feeds)
        assert len(result) == 1
        assert result[0]["title"] == "First"  # first occurrence kept

    def test_empty_list(self):
        assert _dedup([]) == []

    def test_preserves_order(self):
        feeds = [
            {"url": "https://c.com/feed", "title": None},
            {"url": "https://a.com/feed", "title": None},
            {"url": "https://b.com/feed", "title": None},
        ]
        result = _dedup(feeds)
        assert [f["url"] for f in result] == [
            "https://c.com/feed",
            "https://a.com/feed",
            "https://b.com/feed",
        ]


# ── detect_feeds — helpers ────────────────────────────────────────────────────

def _mock_validate():
    """AsyncMock for async_validate_feed_url that does not raise."""
    return AsyncMock(return_value=None)


def _html_with_link(href, mime="application/rss+xml", title=None):
    title_attr = f' title="{title}"' if title else ""
    return f"""<!DOCTYPE html>
<html><head>
<link rel="alternate" type="{mime}" href="{href}"{title_attr}>
</head><body></body></html>"""


# ── detect_feeds — YouTube shortcut ──────────────────────────────────────────

class TestDetectFeedsYoutube:
    async def test_youtube_channel_no_http_fetch(self):
        url = "https://www.youtube.com/channel/UCabcdef1234567890abcdef"
        with patch("app.utils.feed_detect.async_validate_feed_url") as mock_val, \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check") as mock_fetch:
            result = await detect_feeds(url)
        # YouTube shortcut fires before any network call
        mock_val.assert_not_called()
        mock_fetch.assert_not_called()
        assert len(result) == 1
        assert "feeds/videos.xml?channel_id=UCabcdef1234567890abcdef" in result[0]["url"]

    async def test_youtube_user_no_http_fetch(self):
        url = "https://www.youtube.com/user/someuser"
        with patch("app.utils.feed_detect.async_validate_feed_url"), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check") as mock_fetch:
            result = await detect_feeds(url)
        mock_fetch.assert_not_called()
        assert result[0]["url"] == "https://www.youtube.com/feeds/videos.xml?user=someuser"


# ── detect_feeds — <link rel="alternate"> parsing ────────────────────────────

class TestDetectFeedsAlternateLinks:
    async def test_rss_link_returned(self):
        html = _html_with_link("/rss.xml", "application/rss+xml")
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/rss.xml"

    async def test_atom_link_returned(self):
        html = _html_with_link("/atom.xml", "application/atom+xml")
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/atom.xml"

    async def test_title_attribute_included(self):
        html = _html_with_link("/feed.xml", "application/rss+xml", title="Main feed")
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert result[0]["title"] == "Main feed"

    async def test_no_title_attribute_is_none(self):
        html = _html_with_link("/feed.xml", "application/rss+xml")
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert result[0]["title"] is None

    async def test_relative_href_resolved_to_absolute(self):
        html = _html_with_link("feed.xml", "application/rss+xml")
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com/blog/")
        assert result[0]["url"] == "https://example.com/blog/feed.xml"

    async def test_absolute_href_kept_as_is(self):
        html = _html_with_link("https://feeds.example.com/rss", "application/rss+xml")
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert result[0]["url"] == "https://feeds.example.com/rss"

    async def test_multiple_links_all_returned(self):
        html = """<html><head>
            <link rel="alternate" type="application/rss+xml" href="/rss.xml">
            <link rel="alternate" type="application/atom+xml" href="/atom.xml">
        </head></html>"""
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        urls = [f["url"] for f in result]
        assert "https://example.com/rss.xml" in urls
        assert "https://example.com/atom.xml" in urls

    async def test_non_feed_alternate_link_ignored(self):
        html = """<html><head>
            <link rel="alternate" type="text/html" href="/fr/">
            <link rel="alternate" type="application/rss+xml" href="/feed.xml">
        </head></html>"""
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert len(result) == 1
        assert result[0]["url"] == "https://example.com/feed.xml"

    async def test_duplicate_links_deduped(self):
        html = """<html><head>
            <link rel="alternate" type="application/rss+xml" href="/feed.xml">
            <link rel="alternate" type="application/rss+xml" href="/feed.xml">
        </head></html>"""
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", return_value=html):
            result = await detect_feeds("https://example.com")
        assert len(result) == 1


# ── detect_feeds — common path fallback ──────────────────────────────────────

class TestDetectFeedsCommonPaths:
    async def test_fallback_triggered_when_no_alternate_links(self):
        plain_html = "<html><head><title>No feeds here</title></head></html>"

        def fake_fetch(url, **kwargs):
            if url == "https://example.com":
                return plain_html
            if url == "https://example.com/feed":
                return "<?xml version='1.0'?><rss version='2.0'/>"
            raise ValueError("not found")

        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", side_effect=fake_fetch):
            result = await detect_feeds("https://example.com")

        assert any(f["url"] == "https://example.com/feed" for f in result)

    async def test_common_path_without_feed_marker_excluded(self):
        plain_html = "<html><body>no feeds</body></html>"

        def fake_fetch(url, **kwargs):
            return plain_html  # all paths return HTML without feed markers

        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", side_effect=fake_fetch):
            result = await detect_feeds("https://example.com")

        assert result == []

    async def test_common_path_fetch_error_skipped(self):
        plain_html = "<html></html>"

        def fake_fetch(url, **kwargs):
            if url == "https://example.com":
                return plain_html
            raise ValueError("404")

        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", side_effect=fake_fetch):
            result = await detect_feeds("https://example.com")

        assert result == []

    async def test_alternate_links_skip_fallback(self):
        """If <link rel="alternate"> found, common paths are NOT tried."""
        html = _html_with_link("/rss.xml", "application/rss+xml")
        fetch_calls = []

        def fake_fetch(url, **kwargs):
            fetch_calls.append(url)
            return html

        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check", side_effect=fake_fetch):
            result = await detect_feeds("https://example.com")

        # Only the main page was fetched, no common paths
        assert fetch_calls == ["https://example.com"]
        assert len(result) == 1


# ── detect_feeds — error handling ─────────────────────────────────────────────

class TestDetectFeedsErrors:
    async def test_ssrf_validation_failure_returns_empty(self):
        with patch("app.utils.feed_detect.async_validate_feed_url",
                   AsyncMock(side_effect=ValueError("private IP"))):
            result = await detect_feeds("http://192.168.1.1/page")
        assert result == []

    async def test_http_fetch_failure_returns_empty(self):
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check",
                   side_effect=Exception("connection refused")):
            result = await detect_feeds("https://example.com")
        assert result == []

    async def test_malformed_html_does_not_raise(self):
        with patch("app.utils.feed_detect.async_validate_feed_url", _mock_validate()), \
             patch("app.utils.feed_detect.fetch_url_with_ssrf_check",
                   return_value="<<<not html at all>>>"):
            result = await detect_feeds("https://example.com")
        assert isinstance(result, list)

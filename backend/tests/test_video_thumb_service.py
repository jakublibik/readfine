"""Tests for app.services.video_thumb_service: fetching, caching and eviction of
video thumbnails served from our own origin.

The fetch path goes through the real SSRF-safe client (patched with a MockTransport
via mock_httpx_client, plus a public IP for socket.getaddrinfo), so these exercise the
same validation, pinning and size cap the rest of the app fetches through — not a
mock of the fetch itself.
"""
import json
import os
import time
from unittest.mock import patch

import httpx
import pytest

from app.config import settings
from app.services import video_thumb_service as svc
from tests.conftest import mock_httpx_client

# A minimal but valid JPEG signature; the service reads the type from these bytes.
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_PUBLIC_IP = [(2, 1, 6, "", ("93.184.216.34", 0))]


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "thumb_cache_dir", str(tmp_path))
    return tmp_path


@pytest.fixture
def public_dns():
    with patch("socket.getaddrinfo", return_value=_PUBLIC_IP):
        yield


class TestValidate:
    @pytest.mark.parametrize("provider,vid,ok", [
        ("youtube", "dQw4w9WgXcQ", True),
        ("vimeo", "76979871", True),
        ("youtube", "../../evil", False),
        ("vimeo", "not-a-number", False),
        ("dailymotion", "12345678", False),
        ("youtube", "", False),
    ])
    def test_validate(self, provider, vid, ok):
        assert svc._validate(provider, vid) is ok

    def test_invalid_id_never_fetches(self, cache_dir):
        # A bad id must be turned away before any network call — the endpoint's first
        # line of defence against being used as a fetcher.
        with patch.object(svc, "fetch_url_bytes") as fetch:
            assert svc.get_thumbnail("youtube", "../etc/passwd") is None
        fetch.assert_not_called()


class TestImageType:
    @pytest.mark.parametrize("data,expected", [
        (_JPEG, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
        (b"GIF89a" + b"\x00" * 8, "image/gif"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4, "image/webp"),
        (b"<html>not an image</html>", None),
        (b"", None),
    ])
    def test_image_type(self, data, expected):
        assert svc._image_type(data) == expected


class TestGetThumbnailYouTube:
    def test_maxres_success(self, cache_dir, public_dns):
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return httpx.Response(200, content=_JPEG)

        with mock_httpx_client(handler):
            result = svc.get_thumbnail("youtube", "dQw4w9WgXcQ")

        assert result == (_JPEG, "image/jpeg")
        # maxres answered, so hqdefault is never asked for.
        assert seen == ["/vi/dQw4w9WgXcQ/maxresdefault.jpg"]

    def test_falls_back_to_hqdefault_on_maxres_404(self, cache_dir, public_dns):
        def handler(request):
            if "maxresdefault" in request.url.path:
                return httpx.Response(404)
            return httpx.Response(200, content=_JPEG)

        with mock_httpx_client(handler):
            result = svc.get_thumbnail("youtube", "dQw4w9WgXcQ")

        assert result == (_JPEG, "image/jpeg")

    def test_both_sizes_fail_returns_none(self, cache_dir, public_dns):
        def handler(request):
            return httpx.Response(404)

        with mock_httpx_client(handler):
            assert svc.get_thumbnail("youtube", "dQw4w9WgXcQ") is None

    def test_non_image_body_rejected(self, cache_dir, public_dns):
        # A host answering 200 with an HTML error page must not be cached or served
        # as an image (nosniff would break it anyway).
        def handler(request):
            return httpx.Response(200, content=b"<html>error</html>")

        with mock_httpx_client(handler):
            assert svc.get_thumbnail("youtube", "dQw4w9WgXcQ") is None


class TestGetThumbnailVimeo:
    def _oembed_handler(self, thumb_url="https://i.vimeocdn.com/video/123.jpg"):
        # IP pinning rewrites the connect host to the resolved IP, so the transport
        # sees that, not "vimeo.com" — key on the path, which pinning leaves intact.
        def handler(request):
            if request.url.path == "/api/oembed.json":
                return httpx.Response(200, content=json.dumps(
                    {"thumbnail_url": thumb_url}).encode())
            return httpx.Response(200, content=_JPEG)
        return handler

    def test_oembed_then_thumbnail(self, cache_dir, public_dns):
        with mock_httpx_client(self._oembed_handler()):
            result = svc.get_thumbnail("vimeo", "76979871")
        assert result == (_JPEG, "image/jpeg")

    def test_oembed_without_thumbnail_url_returns_none(self, cache_dir, public_dns):
        def handler(request):
            return httpx.Response(200, content=json.dumps({}).encode())
        with mock_httpx_client(handler):
            assert svc.get_thumbnail("vimeo", "76979871") is None

    def test_oembed_broken_json_returns_none(self, cache_dir, public_dns):
        def handler(request):
            return httpx.Response(200, content=b"not json")
        with mock_httpx_client(handler):
            assert svc.get_thumbnail("vimeo", "76979871") is None


class TestCache:
    def test_hit_serves_without_fetching(self, cache_dir, public_dns):
        calls = []

        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(200, content=_JPEG)

        with mock_httpx_client(handler):
            svc.get_thumbnail("youtube", "dQw4w9WgXcQ")  # populate
            # A second call must read the file, not fetch again.
            with patch.object(svc, "fetch_url_bytes") as fetch:
                result = svc.get_thumbnail("youtube", "dQw4w9WgXcQ")
                fetch.assert_not_called()

        assert result == (_JPEG, "image/jpeg")
        assert len(calls) == 1

    def test_corrupt_cache_file_is_dropped(self, cache_dir):
        path = svc._cache_path("youtube", "dQw4w9WgXcQ")
        path.write_bytes(b"junk-not-an-image")
        assert svc._read_cached(path) is None
        assert not path.exists()

    def test_hit_refreshes_access_time(self, cache_dir):
        path = svc._cache_path("youtube", "dQw4w9WgXcQ")
        path.write_bytes(_JPEG)
        old = time.time() - 10_000
        os.utime(path, (old, old))
        svc._read_cached(path)
        assert path.stat().st_atime > old

    def test_size_cap_evicts_least_recently_used(self, cache_dir, monkeypatch):
        monkeypatch.setattr(settings, "thumb_cache_max_mb", 1)
        blob = b"\xff\xd8\xff\xe0" + b"\x00" * (600 * 1024)  # ~600 kB each
        now = time.time()
        for i, name in enumerate(["youtube_a", "youtube_b", "youtube_c"]):
            p = cache_dir / name
            p.write_bytes(blob)
            # a is oldest, c is newest.
            os.utime(p, (now - (30 - i * 10) * 60, now))
        svc._enforce_size_cap()
        # Two 600 kB files already exceed the 1 MB cap, so the oldest is dropped.
        assert not (cache_dir / "youtube_a").exists()
        assert (cache_dir / "youtube_c").exists()

    def test_idle_sweep_drops_only_stale_entries(self, cache_dir, monkeypatch):
        monkeypatch.setattr(settings, "thumb_cache_idle_days", 30)
        fresh = cache_dir / "youtube_fresh"
        stale = cache_dir / "youtube_stale"
        fresh.write_bytes(_JPEG)
        stale.write_bytes(_JPEG)
        now = time.time()
        os.utime(fresh, (now, now))
        os.utime(stale, (now - 40 * 86400, now - 40 * 86400))
        removed = svc.sweep_idle_thumbnails()
        assert removed == 1
        assert fresh.exists()
        assert not stale.exists()

    def test_idle_sweep_disabled_when_zero(self, cache_dir, monkeypatch):
        monkeypatch.setattr(settings, "thumb_cache_idle_days", 0)
        stale = cache_dir / "youtube_stale"
        stale.write_bytes(_JPEG)
        os.utime(stale, (time.time() - 999 * 86400,) * 2)
        assert svc.sweep_idle_thumbnails() == 0
        assert stale.exists()


class TestNegativeCache:
    def test_failure_is_remembered_and_not_refetched(self, cache_dir, public_dns):
        # First lookup fails at the host; the second within the TTL must answer from
        # the miss marker on disk, never going back out.
        def handler(request):
            return httpx.Response(404)

        with mock_httpx_client(handler):
            assert svc.get_thumbnail("youtube", "dQw4w9WgXcQ") is None

        with patch.object(svc, "fetch_url_bytes") as fetch:
            assert svc.get_thumbnail("youtube", "dQw4w9WgXcQ") is None
            fetch.assert_not_called()

    def test_stale_miss_marker_expires_and_is_cleared(self, cache_dir):
        svc._write_miss("youtube", "dQw4w9WgXcQ")
        path = svc._miss_path("youtube", "dQw4w9WgXcQ")
        old = time.time() - svc._NEG_CACHE_TTL - 1
        os.utime(path, (old, old))
        assert svc._recent_miss("youtube", "dQw4w9WgXcQ") is False
        # An expired marker is dropped, so the next request tries the host again.
        assert not path.exists()

    def test_sweep_clears_expired_miss_markers(self, cache_dir, monkeypatch):
        monkeypatch.setattr(settings, "thumb_cache_idle_days", 30)
        svc._write_miss("vimeo", "76979871")
        path = svc._miss_path("vimeo", "76979871")
        old = time.time() - svc._NEG_CACHE_TTL - 10
        os.utime(path, (old, old))
        assert svc.sweep_idle_thumbnails() >= 1
        assert not path.exists()

    def test_miss_marker_is_not_counted_as_a_thumbnail(self, cache_dir):
        # A miss marker must not be served as an image or counted by the size cap.
        svc._write_miss("youtube", "dQw4w9WgXcQ")
        assert svc._cache_entries() == []


class TestEndpoint:
    """The public route. Unauthenticated on purpose (shared article pages render
    video figures for signed-out readers); safe because only a fixed provider set and
    a validated id reach the service."""

    def test_serves_image_with_immutable_cache(self, client):
        with patch.object(svc, "get_thumbnail", return_value=(_JPEG, "image/jpeg")):
            resp = client.get("/img/video-thumb/youtube/dQw4w9WgXcQ")
        assert resp.status_code == 200
        assert resp.content == _JPEG
        assert resp.headers["content-type"] == "image/jpeg"
        assert "immutable" in resp.headers["cache-control"]
        assert resp.headers["etag"] == '"vt-youtube-dQw4w9WgXcQ"'

    def test_missing_thumbnail_is_404(self, client):
        with patch.object(svc, "get_thumbnail", return_value=None):
            resp = client.get("/img/video-thumb/youtube/dQw4w9WgXcQ")
        assert resp.status_code == 404

    def test_if_none_match_returns_304_without_fetching(self, client):
        etag = '"vt-youtube-dQw4w9WgXcQ"'
        with patch.object(svc, "get_thumbnail") as get:
            resp = client.get(
                "/img/video-thumb/youtube/dQw4w9WgXcQ",
                headers={"If-None-Match": etag},
            )
            get.assert_not_called()
        assert resp.status_code == 304
        assert resp.headers["etag"] == etag

    def test_available_without_authentication(self, unauth_client):
        # A signed-out reader on a /share/ page must still get the thumbnail.
        with patch.object(svc, "get_thumbnail", return_value=(_JPEG, "image/jpeg")):
            resp = unauth_client.get("/img/video-thumb/youtube/dQw4w9WgXcQ")
        assert resp.status_code == 200

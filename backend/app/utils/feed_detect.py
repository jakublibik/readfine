"""Feed URL detection from HTML pages."""
import asyncio
import re
from urllib.parse import urljoin, urlparse

from lxml import html

from app.utils.url_validator import async_validate_feed_url, fetch_url_with_ssrf_check

_FEED_MIME_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "text/xml",
    "application/xml",
}
_COMMON_PATHS = ["/feed", "/rss", "/rss.xml", "/atom.xml", "/feed.xml", "/feeds/posts/default"]
_FEED_MARKERS = ("<rss", "<feed", "<atom:feed", "<?xml")
_FETCH_HEADERS = {"User-Agent": "Readfine/1.0", "Accept": "text/html,*/*"}

_YT_CHANNEL_RE = re.compile(r"youtube\.com/channel/(UC[\w-]+)")
_YT_USER_RE = re.compile(r"youtube\.com/user/([\w-]+)")


def _youtube_feed_url(url: str) -> str | None:
    if m := _YT_CHANNEL_RE.search(url):
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}"
    if m := _YT_USER_RE.search(url):
        return f"https://www.youtube.com/feeds/videos.xml?user={m.group(1)}"
    return None


async def detect_feeds(url: str) -> list[dict]:
    """
    Detect RSS/Atom feeds linked from a web page.
    Returns list of {"url": str, "title": str | None}.
    """
    # YouTube shortcut — channel ID is in the URL, no fetch needed
    if yt_url := _youtube_feed_url(url):
        return [{"url": yt_url, "title": "YouTube channel feed"}]

    # Fetch HTML
    try:
        await async_validate_feed_url(url)
        loop = asyncio.get_running_loop()
        content = await loop.run_in_executor(
            None,
            lambda: fetch_url_with_ssrf_check(url, headers=_FETCH_HEADERS, timeout=15),
        )
    except Exception:
        return []

    results: list[dict] = []

    # Parse <link rel="alternate"> from HTML
    try:
        tree = html.fromstring(content.encode())
        for link in tree.xpath('//link[@rel="alternate"]'):
            ltype = (link.get("type") or "").lower().split(";")[0].strip()
            if ltype in _FEED_MIME_TYPES:
                href = (link.get("href") or "").strip()
                if href:
                    results.append({
                        "url": urljoin(url, href),
                        "title": link.get("title") or None,
                    })
    except Exception:
        pass

    if results:
        return _dedup(results)

    # Fallback: try common paths in parallel
    origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"

    async def _try_path(path: str) -> dict | None:
        candidate = origin + path
        try:
            await async_validate_feed_url(candidate)
            _loop = asyncio.get_running_loop()
            body = await _loop.run_in_executor(
                None,
                lambda: fetch_url_with_ssrf_check(candidate, headers=_FETCH_HEADERS, timeout=10),
            )
            if any(marker in body[:500] for marker in _FEED_MARKERS):
                return {"url": candidate, "title": None}
        except Exception:
            pass
        return None

    found = await asyncio.gather(*[_try_path(p) for p in _COMMON_PATHS])
    results.extend(r for r in found if r is not None)
    return _dedup(results)


def _dedup(feeds: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for f in feeds:
        if f["url"] not in seen:
            seen.add(f["url"])
            out.append(f)
    return out

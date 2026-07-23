"""Web scrape fetcher: CSS selector → article URLs → readable extraction pipeline."""
import asyncio
import hashlib
import html
import logging
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.feed import Feed
from app.models.fetch_log import FetchLog
from app.utils.http_client import READFINE_UA
from app.fetcher import host_throttle
from app.utils.url_validator import (
    async_validate_feed_url,
    fetch_url_page,
    fetch_url_with_ssrf_check,
    redact_url,
)
from app.fetcher.redirects import adopt_permanent_url
# FETCH_ERROR_DISABLE_THRESHOLD is re-exported for symmetry with rss.py.
from app.fetcher.failure import (  # noqa: F401
    FETCH_ERROR_DISABLE_THRESHOLD,
    arm_host_cooldown,
    failure_message,
    failure_values,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": READFINE_UA,
    "Accept": "text/html,application/xhtml+xml,*/*",
}
_TIMEOUT = 30

_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "msclkid",
})


def _normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return None
        path = p.path.rstrip("/") or "/"
        params = [(k, v) for k, v in sorted(parse_qsl(p.query)) if k not in _STRIP_PARAMS]
        return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", urlencode(params), ""))[:2048]
    except Exception:
        return None


async def fetch_page_html(url: str, timeout: int = 30) -> str:
    """SSRF-safe fetch of a page's raw HTML for scrape setup / preview / validation.

    Runs off the event loop and uses the scrape fetcher's own headers (READFINE_UA),
    so a page tested during selector setup fetches identically when actually scraped.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, fetch_url_with_ssrf_check, url, None, timeout, _HEADERS
    )


def _extract_title(elem, a_tag, fallback_url: str) -> str:
    # 1. Heading inside the <a> tag itself (avoids picking up sibling/parent section labels)
    heading = a_tag.find(["h1", "h2", "h3", "h4"])
    if heading:
        text = heading.get_text(strip=True)
        if text:
            return text[:500]
    # 2. Heading inside the broader container (when <a> contains no heading)
    if elem is not a_tag:
        heading = elem.find(["h1", "h2", "h3", "h4"])
        if heading:
            text = heading.get_text(strip=True)
            if text:
                return text[:500]
    # 3. Text of the <a> tag itself
    a_text = a_tag.get_text(strip=True)
    if a_text:
        return a_text[:500]
    # 4. title / aria-label attributes
    for attr in ("title", "aria-label"):
        val = (a_tag.get(attr) or elem.get(attr) or "").strip()
        if val:
            return val[:500]
    # 5. alt text of first image inside the link
    img = a_tag.find("img")
    if img:
        alt = (img.get("alt") or "").strip()
        if alt:
            return alt[:500]
    return fallback_url


_URL_DATE_RE = re.compile(r"_(\d{10})_")


def _published_at_from_url(url: str) -> datetime | None:
    """Extract date from URLs with embedded timestamp like _YYMMDDHHMM_ (iRozhlas, ČT24…)."""
    m = _URL_DATE_RE.search(url)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _extract_published_at(elem) -> datetime | None:
    # Accept datetime attribute on any tag (<time>, <span>, <div>, …)
    tag = elem.find(attrs={"datetime": True})
    if not tag:
        return None
    raw = (tag.get("datetime") or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            return None  # naive datetime — unknown timezone, don't guess
        return dt
    except ValueError:
        return None


def _extract_excerpt(elem, title_text: str) -> str | None:
    for p in elem.find_all("p"):
        text = p.get_text(strip=True)
        if len(text) < 30:
            continue
        if title_text and text.lower() == title_text.lower():
            continue
        if text.count("|") >= 2:
            continue
        return text[:500]
    return None


def _excerpt_to_content_html(excerpt: str | None) -> str | None:
    """Wrap a scraped excerpt as stored content HTML.

    `excerpt` is decoded plain text (BeautifulSoup get_text), so it must be
    HTML-escaped before wrapping — otherwise injected markup is stored verbatim
    and rendered `| safe` at read time (stored XSS). Mirrors the RSS path's nh3
    sanitize.
    """
    if not excerpt:
        return None
    return f"<p>{html.escape(excerpt)}</p>"


_CONTAINER_TAGS = {"article", "li", "div", "section"}


def _metadata_context(elem):
    """Return the element to use for metadata extraction.
    When the selector matched an <a> directly, walk up to the nearest
    container so that <time> and <p> siblings are reachable."""
    if elem.name != "a":
        return elem
    for parent in elem.parents:
        if getattr(parent, "name", None) in _CONTAINER_TAGS:
            return parent
    return elem


def extract_article_links(
    html: str, selector: str, feed_url: str
) -> list[tuple[str, str, datetime | None, str | None]]:
    """Apply CSS selector, return (url, title, published_at, excerpt) tuples."""
    soup = BeautifulSoup(html, "lxml")
    results: list[tuple[str, str, datetime | None, str | None]] = []
    seen_urls: set[str] = set()
    for elem in soup.select(selector)[:100]:
        a = elem if elem.name == "a" else elem.find("a")
        if not a or not a.get("href"):
            continue
        href = str(a.get("href", "")).strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(feed_url, href)
        if not url.startswith(("http://", "https://")):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        ctx = _metadata_context(elem)
        title = _extract_title(ctx, a, url)
        published_at = _extract_published_at(ctx) or _published_at_from_url(url)
        excerpt = _extract_excerpt(ctx, title)
        results.append((url, title, published_at, excerpt))
    return results


async def fetch_scrape_feed(
    feed: Feed, db: AsyncSession, published_cutoff: datetime | None = None
) -> int:
    """Fetch a scrape-type feed via CSS selector. Returns number of new articles.

    Note: HTTP auth credentials are intentionally not supported for scrape feeds.
    Sites requiring auth typically use session cookies or JS challenges, not HTTP Basic Auth.
    """
    start_ms = int(time.monotonic() * 1000)
    feed_id = feed.id
    feed_url = feed.feed_url
    # Read before any DB work: the error path rolls back first, which expires the
    # instance, and re-reading an attribute there would fire a lazy load. The same
    # goes for is_private, which the post-commit URL adoption needs.
    block_count = feed.block_count or 0
    is_private = bool(feed.is_private)
    selector = (feed.type_config or {}).get("article_links_selector", "")
    fetched_at = datetime.now(timezone.utc)

    try:
        # Re-validate the stored URL on every fetch (matches the RSS fetcher):
        # a hostname that resolved publicly at feed creation could later point
        # at an internal/metadata address.
        await async_validate_feed_url(feed_url)
        loop = asyncio.get_running_loop()
        page = await loop.run_in_executor(
            None, fetch_url_page, feed_url, None, _TIMEOUT, _HEADERS
        )
        links = extract_article_links(page.text, selector, feed_url)
        if not links:
            raise ValueError(f"CSS selector '{selector}' matched no article links")

        new_count = await _save_scrape_articles(feed, links, fetched_at, db, published_cutoff)
        duration_ms = int(time.monotonic() * 1000) - start_ms

        feed.last_fetched_at = fetched_at
        feed.last_fetch_duration_ms = duration_ms
        feed.status = "active"
        feed.last_error = None
        feed.fetch_error_count = 0
        feed.block_count = 0
        feed.retry_after_until = None
        # Mirror rss.py: track the newest article date this listing carried. Only
        # advance when at least one link is dated, so a fetch of purely undated
        # links doesn't wipe a previously-known publication date. Stays None for
        # feeds whose listings never expose dates (genuinely unknown, not fetch time).
        latest_pub = max((pub for _, _, pub, _ in links if pub), default=None)
        if latest_pub:
            feed.last_published_at = latest_pub
        await db.commit()
        # Scrape success carries no RateLimit-* headers (fetch returns HTML only), so
        # this just clears any pending 429 streak for the host.
        host_throttle.record_success(host_throttle.host_key(feed_url), datetime.now(timezone.utc))
        # Only after the selector actually matched links, so a redirect to a page
        # this feed cannot scrape never becomes its stored address.
        if page.permanent_url:
            await adopt_permanent_url(
                feed_id, feed_url, page.permanent_url, db,
                feed_type="scrape", selector=selector, is_private=is_private,
            )
        logger.info("Scrape feed %d: %d new articles in %dms", feed_id, new_count, duration_ms)
        return new_count

    except Exception as exc:
        await db.rollback()
        logger.error("Error scraping feed %d (%s): %s", feed_id, redact_url(feed_url), exc)
        now = datetime.now(timezone.utc)
        http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        db.add(FetchLog(
            feed_id=feed_id,
            failed_at=now,
            http_status=http_status,
            error_message=failure_message(exc, feed_url),
        ))
        arm_host_cooldown(feed_url, exc, http_status, now)
        await db.execute(
            update(Feed).where(Feed.id == feed_id).values(
                **failure_values(exc, feed_url=feed_url, feed_block_count=block_count, now=now)
            )
        )
        await db.commit()
        return 0


async def _save_scrape_articles(
    feed: Feed,
    links: list[tuple[str, str, datetime | None, str | None]],
    fetched_at: datetime,
    db: AsyncSession,
    published_cutoff: datetime | None = None,
) -> int:
    urls = [url for url, *_ in links]
    guid_hash_map = {url: hashlib.sha256(url.encode()).hexdigest() for url in urls}
    norm_map = {url: _normalize_url(url) for url in urls}

    existing_hashes: set[str] = set(
        (await db.execute(
            select(Article.guid_hash).where(
                Article.feed_id == feed.id,
                Article.guid_hash.in_(guid_hash_map.values()),
            )
        )).scalars()
    )

    norm_urls = [n for n in norm_map.values() if n]
    existing_normalized: set[str] = set()
    if norm_urls:
        existing_normalized = set(
            (await db.execute(
                select(Article.url_normalized).where(
                    Article.feed_id == feed.id,
                    Article.url_normalized.in_(norm_urls),
                )
            )).scalars()
        )

    new_articles: list[Article] = []
    for url, title, pub_at, excerpt in links:
        # Skip dated links older than the purge cutoff — prevents re-inserting
        # purgeable articles (mirrors rss.py). Undated links (pub_at is None) store
        # published_at=None (ordering/purge fall back to fetched_at via coalesce, as
        # in rss.py) and can't be cutoff-filtered, so they may re-cycle after purge;
        # fully solving that would require URL tombstones.
        if published_cutoff is not None and pub_at is not None and pub_at < published_cutoff:
            continue
        gh = guid_hash_map[url]
        if gh in existing_hashes:
            continue
        nu = norm_map[url]
        if nu and nu in existing_normalized:
            continue
        existing_hashes.add(gh)
        if nu:
            existing_normalized.add(nu)

        content_html = _excerpt_to_content_html(excerpt)
        new_articles.append(Article(
            feed_id=feed.id,
            guid=url[:2048],
            guid_hash=gh,
            url=url[:2048],
            url_normalized=nu,
            title=title[:1000],
            content=content_html,
            content_source="feed" if content_html else "skipped",
            readable_status="skipped",
            # None when the listing carried no date — matches rss.py; ordering and
            # purge fall back to fetched_at via coalesce, and readable extraction
            # (auto on labeled articles, on-demand otherwise) backfills the real
            # publication date from the article page via htmldate.
            published_at=pub_at,
            fetched_at=fetched_at,
        ))

    if new_articles:
        for a in new_articles:
            db.add(a)
        await db.flush()
        from app.services.filter_service import apply_filters_to_new_articles
        await apply_filters_to_new_articles(feed.id, new_articles, db)

    return len(new_articles)

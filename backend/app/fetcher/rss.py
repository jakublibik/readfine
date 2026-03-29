"""RSS feed fetcher: HTTP fetch + feedparser + article deduplication."""
import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone

import feedparser
import httpx
import nh3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.feed import Feed, UserFeed
from app.models.fetch_log import FetchLog
from app.utils.crypto import decrypt
from app.utils.url_validator import validate_feed_url

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Filtread/1.0 (+https://github.com/filtread/filtread)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
_TIMEOUT = 30  # seconds


async def fetch_and_parse_url(url: str) -> feedparser.FeedParserDict:
    """Fetch a URL and parse it as RSS/Atom. Raises on HTTP or parse failure."""
    validate_feed_url(url)
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
        response = await client.get(url, headers=_HEADERS)
        response.raise_for_status()
        content = response.text

    loop = asyncio.get_event_loop()
    parsed = await loop.run_in_executor(None, feedparser.parse, content)

    if parsed.bozo and not parsed.entries and not parsed.feed:
        raise ValueError(f"Not a valid RSS/Atom feed: {parsed.bozo_exception}")

    return parsed


async def fetch_feed(feed: Feed, db: AsyncSession) -> int:
    """Fetch a feed and store new articles. Returns number of new articles saved."""
    start_ms = int(time.monotonic() * 1000)

    try:
        validate_feed_url(feed.feed_url)
        auth = None
        if feed.fetch_auth_user and feed.fetch_auth_pass_encrypted:
            auth = (feed.fetch_auth_user, decrypt(feed.fetch_auth_pass_encrypted))
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True, max_redirects=5) as client:
            response = await client.get(feed.feed_url, headers=_HEADERS, auth=auth)
            response.raise_for_status()
            content = response.text

        loop = asyncio.get_event_loop()
        parsed = await loop.run_in_executor(None, feedparser.parse, content)

        if parsed.bozo and not parsed.entries:
            raise ValueError(f"Feed parse error: {parsed.bozo_exception}")

        new_count = await _save_articles(feed, parsed, db)
        duration_ms = int(time.monotonic() * 1000) - start_ms

        feed.last_fetched_at = datetime.now(timezone.utc)
        feed.last_fetch_duration_ms = duration_ms
        feed.status = "active"
        feed.last_error = None

        latest_pub = _latest_published(parsed.entries)
        if latest_pub:
            feed.last_published_at = latest_pub

        await db.commit()
        logger.info("Fetched feed %d: %d new articles in %dms", feed.id, new_count, duration_ms)
        return new_count

    except Exception as exc:
        logger.error("Error fetching feed %d (%s): %s", feed.id, feed.feed_url, exc)
        http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        feed.status = "error"
        feed.last_error = str(exc)[:500]
        feed.last_fetched_at = datetime.now(timezone.utc)
        db.add(FetchLog(
            feed_id=feed.id,
            failed_at=datetime.now(timezone.utc),
            http_status=http_status,
            error_message=str(exc)[:500],
        ))
        await db.commit()
        return 0


async def _save_articles(feed: Feed, parsed: feedparser.FeedParserDict, db: AsyncSession) -> int:
    """Insert new articles from parsed feed, apply filters. Returns count of inserted articles."""
    # Determine if any subscriber wants readable extraction
    result = await db.execute(
        select(UserFeed.id).where(
            UserFeed.feed_id == feed.id,
            UserFeed.extract_readable == True,  # noqa: E712
        ).limit(1)
    )
    wants_readable = result.scalar_one_or_none() is not None

    new_articles: list[Article] = []
    for entry in parsed.entries:
        guid = (entry.get("id") or entry.get("link") or entry.get("title") or "")
        if not guid:
            continue

        guid_hash = hashlib.sha256(guid.encode()).hexdigest()

        existing = await db.execute(
            select(Article.id).where(
                Article.feed_id == feed.id,
                Article.guid_hash == guid_hash,
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue

        content, content_source = _extract_content(entry)
        if content:
            content = nh3.clean(content)

        word_count, estimated_read_min = _reading_stats(content)

        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = _struct_to_dt(pub) if pub else None

        article = Article(
            feed_id=feed.id,
            guid=guid[:2048],
            guid_hash=guid_hash,
            url=_safe_url(entry.get("link")),
            title=(entry.get("title") or "Untitled")[:1000],
            author=_extract_author(entry),
            content=content,
            content_source=content_source,
            readable_status="pending" if wants_readable else "skipped",
            published_at=published_at,
            word_count=word_count,
            estimated_read_min=estimated_read_min,
            image_url=_extract_image(entry),
        )
        db.add(article)
        new_articles.append(article)

    if new_articles:
        # Flush to get IDs, then apply filters before the outer commit
        await db.flush()
        from app.services.filter_service import apply_filters_to_article
        for article in new_articles:
            await apply_filters_to_article(article, db)

        # Auto-detect full-content feed and disable readable extraction if warranted
        if wants_readable:
            from app.services.readable_service import maybe_disable_readable_for_feed
            await maybe_disable_readable_for_feed(feed.id, db)

    return len(new_articles)


# ── helpers ──────────────────────────────────────────────────────────────────

def _extract_content(entry) -> tuple[str | None, str | None]:
    if entry.get("content"):
        for c in entry.content:
            if c.get("value"):
                return c.value, "feed_content"
    if entry.get("summary"):
        return entry.summary, "feed_summary"
    return None, None


def _extract_author(entry) -> str | None:
    author = entry.get("author")
    if author:
        return author[:255]
    authors = entry.get("authors") or []
    if authors:
        name = (authors[0].get("name") or "").strip()
        return name[:255] or None
    return None


def _extract_image(entry) -> str | None:
    for key in ("media_thumbnail", "media_content"):
        media = entry.get(key)
        if media and isinstance(media, list) and media[0].get("url"):
            return _safe_url(media[0]["url"])
    for enc in entry.get("enclosures") or []:
        if (enc.get("type") or "").startswith("image/") and enc.get("href"):
            return _safe_url(enc["href"])
    return None


def _reading_stats(content: str | None) -> tuple[int | None, int | None]:
    if not content:
        return None, None
    plain = nh3.clean(content, tags=set())
    words = len(re.findall(r"\w+", plain))
    return words, max(1, round(words / 200))


def _struct_to_dt(t) -> datetime:
    return datetime(*t[:6], tzinfo=timezone.utc)


def _latest_published(entries) -> datetime | None:
    dates = []
    for e in entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            dates.append(_struct_to_dt(t))
    return max(dates) if dates else None


def _val_or_none(value: str | None, max_len: int) -> str | None:
    if not value:
        return None
    return value[:max_len]


def _safe_url(value: str | None, max_len: int = 2048) -> str | None:
    """Allow only http/https URLs to prevent javascript: and other dangerous schemes."""
    if not value:
        return None
    stripped = value.strip()
    if not stripped.lower().startswith(("http://", "https://")):
        return None
    return stripped[:max_len]

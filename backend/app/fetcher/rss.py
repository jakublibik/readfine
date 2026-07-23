"""RSS feed fetcher: HTTP fetch + feedparser + article deduplication."""
import asyncio
import hashlib
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
import nh3
from sqlalchemy import literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.fetch_log import FetchLog
from app.utils.crypto import decrypt
from app.utils.http_client import READFINE_UA
from app.utils.url_validator import (
    async_validate_feed_url,
    fetch_url_conditional,
    fetch_url_page,
    redact_url,
    validate_feed_url,
)
from app.fetcher import host_throttle
from app.fetcher.redirects import adopt_permanent_url
# FETCH_ERROR_DISABLE_THRESHOLD is re-exported: the scheduler and tests import it from here.
from app.fetcher.failure import (  # noqa: F401
    FETCH_ERROR_DISABLE_THRESHOLD,
    arm_host_cooldown,
    failure_values,
)

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": READFINE_UA,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
_TIMEOUT = 30  # seconds

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



class ParsedFeed(NamedTuple):
    """A parsed feed plus the address it permanently moved to, if any.

    ``permanent_url`` lets the subscribe path create the feed row on the address
    the host actually serves, instead of one that redirects on every later poll.
    """
    parsed: feedparser.FeedParserDict
    permanent_url: str | None


async def fetch_and_parse_url(url: str) -> ParsedFeed:
    """Fetch a URL and parse it as RSS/Atom. Raises on HTTP or parse failure."""
    await async_validate_feed_url(url)
    loop = asyncio.get_running_loop()
    page = await loop.run_in_executor(
        None, fetch_url_page, url, None, _TIMEOUT, _HEADERS
    )
    parsed = await loop.run_in_executor(None, feedparser.parse, page.text)

    if parsed.bozo:
        import xml.sax._exceptions as _sax
        if isinstance(parsed.bozo_exception, _sax.SAXParseException):
            # XML parse error means the response is HTML, not RSS
            raise ValueError(f"Not a valid RSS/Atom feed: {parsed.bozo_exception}")
        if not parsed.entries and not parsed.feed:
            raise ValueError(f"Not a valid RSS/Atom feed: {parsed.bozo_exception}")

    return ParsedFeed(parsed, page.permanent_url)


def cooldown_until(feed: Feed, now: datetime) -> datetime | None:
    """Latest active rate-limit cooldown for a *manual* fetch of this feed, or None.

    Combines the per-feed ``retry_after_until`` (persisted in the DB) with the per-host
    in-memory throttle (armed when *any* feed on the same host was rate-limited). Manual
    fetch paths consult this to avoid hammering into a known 429 window; the scheduler
    enforces the same via its own gates. Returns the later of the two, or None when
    neither is active.

    A feed deferred by the *block* backoff is excluded: that deadline is our own guess
    about a host refusing automation, not something the host asked for, and the block is
    often transient (measured: a third of requests refused, in waves). Someone clicking
    Refresh is explicitly asking to find out, and may well get through. Any deadline the
    host really did state — a ``Retry-After`` or a ``RateLimit-*`` reset — is armed on the
    host throttle by ``arm_host_cooldown`` as well, so it still gates manual fetches here.
    """
    candidates = []
    blocked = (feed.block_count or 0) > 0
    if not blocked and feed.retry_after_until is not None and feed.retry_after_until > now:
        candidates.append(feed.retry_after_until)
    host_until = host_throttle.blocked_until(host_throttle.host_key(feed.feed_url), now)
    if host_until is not None:
        candidates.append(host_until)
    return max(candidates) if candidates else None


async def fetch_feed(
    feed: Feed,
    db: AsyncSession,
    initial_limit: int | None = None,
    published_cutoff: datetime | None = None,
    prefetched: feedparser.FeedParserDict | None = None,
) -> int:
    """Fetch a feed and store new articles. Returns number of new articles saved.

    *prefetched*: an already-fetched+parsed feed (e.g. from the subscribe/test step)
    to reuse instead of downloading again — keeps the subscribe flow to a single
    network request for rate-limited sites.
    """
    start_ms = int(time.monotonic() * 1000)
    feed_id = feed.id
    feed_url = feed.feed_url
    # Read before any DB work: the error path rolls back first, which expires the
    # instance, and re-reading an attribute there would fire a lazy load. The same
    # goes for is_private, which the post-commit URL adoption needs.
    block_count = feed.block_count or 0
    is_private = bool(feed.is_private)

    try:
        resp = None
        if prefetched is not None:
            parsed = prefetched
        else:
            await async_validate_feed_url(feed_url)
            auth = None
            if feed.fetch_auth_user and feed.fetch_auth_pass_encrypted:
                auth = (feed.fetch_auth_user, decrypt(feed.fetch_auth_pass_encrypted))
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(
                None, fetch_url_conditional, feed_url, auth, _TIMEOUT, _HEADERS,
                feed.etag, feed.last_modified,
            )
            if resp.status_code == 304:
                # Unchanged since last fetch — no body to parse. Record a successful
                # poll and keep the stored validators.
                feed.last_fetched_at = datetime.now(timezone.utc)
                feed.last_fetch_duration_ms = int(time.monotonic() * 1000) - start_ms
                feed.status = "active"
                feed.last_error = None
                feed.fetch_error_count = 0
                feed.block_count = 0
                feed.retry_after_until = None
                await db.commit()
                host = host_throttle.host_key(feed_url)
                if resp.rate_limited_until:
                    host_throttle.note_rate_limited(host, resp.rate_limited_until)
                host_throttle.record_success(host, datetime.now(timezone.utc), resp.spacing_seconds)
                if resp.permanent_url:
                    await adopt_permanent_url(
                        feed_id, feed_url, resp.permanent_url, db, is_private=is_private
                    )
                logger.info("Feed %d not modified (304)", feed_id)
                return 0
            parsed = await loop.run_in_executor(None, feedparser.parse, resp.text)

        if parsed.bozo and not parsed.entries:
            raise ValueError(f"Feed parse error: {parsed.bozo_exception}")

        new_count = await _save_articles(feed, parsed, db, limit=initial_limit, published_cutoff=published_cutoff)
        duration_ms = int(time.monotonic() * 1000) - start_ms

        feed.last_fetched_at = datetime.now(timezone.utc)
        feed.last_fetch_duration_ms = duration_ms
        feed.status = "active"
        feed.last_error = None
        feed.fetch_error_count = 0
        feed.block_count = 0
        feed.retry_after_until = None
        # Update validators from this 200, but keep the last-known ones when the
        # response omits a header (some CDNs send ETag only intermittently) so we
        # don't lose the ability to make conditional requests.
        if resp is not None:
            if resp.etag:
                feed.etag = resp.etag
            if resp.last_modified:
                feed.last_modified = resp.last_modified

        latest_pub = _latest_published(parsed.entries)
        if latest_pub:
            feed.last_published_at = latest_pub

        await db.commit()
        if resp is not None:
            host = host_throttle.host_key(feed_url)
            if resp.rate_limited_until:
                host_throttle.note_rate_limited(host, resp.rate_limited_until)
            host_throttle.record_success(host, datetime.now(timezone.utc), resp.spacing_seconds)
            # Only now that the body parsed and its articles are committed: a dead
            # feed 301'ing to the site homepage must not become the stored address.
            if resp.permanent_url:
                await adopt_permanent_url(
                    feed_id, feed_url, resp.permanent_url, db, is_private=is_private
                )
        logger.info("Fetched feed %d: %d new articles in %dms", feed_id, new_count, duration_ms)
        return new_count

    except IntegrityError as exc:
        # Benign concurrent-fetch race: another worker inserted the same
        # (feed_id, guid_hash) between our dedup SELECT and flush. Not a feed
        # failure — roll back and let the next scheduled fetch pick them up.
        await db.rollback()
        logger.info("Concurrent duplicate insert for feed %d, skipping round: %s", feed_id, exc)
        return 0

    except Exception as exc:
        await db.rollback()
        logger.error("Error fetching feed %d (%s): %s", feed_id, redact_url(feed_url), exc)
        now = datetime.now(timezone.utc)
        http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        db.add(FetchLog(
            feed_id=feed_id,
            failed_at=now,
            http_status=http_status,
            error_message=str(exc)[:500],
        ))
        arm_host_cooldown(feed_url, exc, http_status, now)
        await db.execute(
            update(Feed).where(Feed.id == feed_id).values(
                **failure_values(exc, feed_block_count=block_count, now=now)
            )
        )
        await db.commit()
        return 0


async def _save_articles(
    feed: Feed,
    parsed: feedparser.FeedParserDict,
    db: AsyncSession,
    limit: int | None = None,
    published_cutoff: datetime | None = None,
) -> int:
    """Insert new articles from parsed feed, apply filters. Returns count of inserted articles."""
    entries = parsed.entries[:limit] if limit is not None else parsed.entries

    # Deduplicate within this batch and compute guid_hashes up front
    candidates: dict[str, feedparser.util.FeedParserDict] = {}  # hash → entry (first wins)
    for entry in entries:
        raw_guid = (entry.get("id") or entry.get("link") or entry.get("title") or "")
        if not raw_guid:
            continue
        # Skip entries older than the purge cutoff — prevents re-inserting purgeable articles
        # after they've been purged from the DB and the feed XML still contains them.
        if published_cutoff is not None:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub:
                pub_dt = _struct_to_dt(pub)
                if pub_dt is not None and pub_dt < published_cutoff:
                    continue
        guid = _normalize_guid(raw_guid)
        guid_hash = hashlib.sha256(guid.encode()).hexdigest()
        candidates.setdefault(guid_hash, entry)

    if not candidates:
        return 0

    # Bulk-check which hashes already exist — one query, no per-article SELECTs,
    # which prevents autoflush of not-yet-committed articles (race condition guard).
    existing_result = await db.execute(
        select(Article.guid_hash).where(
            Article.feed_id == feed.id,
            Article.guid_hash.in_(candidates.keys()),
        )
    )
    existing_hashes: set[str] = set(existing_result.scalars())

    # Secondary dedup by URL — catches feeds that rotate GUIDs on updates (e.g. BBC).
    # Only URLs that identify a single item in this batch are usable as a dedup key
    # (see _url_dedup_keys): a link shared by several items is a section/show-level
    # URL (e.g. podcast episodes all pointing at the show page), so deduping on it
    # would silently drop every new item after the first, since the shared URL is
    # already in the DB while each item's GUID is unique.
    url_dedup_keys = _url_dedup_keys(
        _safe_url(e.get("link")) for e in candidates.values()
    )
    existing_urls: set[str] = set()
    if url_dedup_keys:
        url_result = await db.execute(
            select(Article.url).where(
                Article.feed_id == feed.id,
                Article.url.in_(url_dedup_keys),
            )
        )
        existing_urls = set(url_result.scalars())

    fetched_at = datetime.now(timezone.utc)
    new_articles: list[Article] = []
    for guid_hash, entry in candidates.items():
        if guid_hash in existing_hashes:
            continue
        article_url = _safe_url(entry.get("link"))
        if article_url and article_url in existing_urls:
            continue

        guid = _normalize_guid(entry.get("id") or entry.get("link") or entry.get("title") or "")
        content, content_source = _extract_content(entry)
        if content:
            content = nh3.clean(content)
            if article_url:
                from app.utils.parsing import rewrite_relative_urls
                content = rewrite_relative_urls(content, article_url)

        word_count, estimated_read_min = _reading_stats(content)
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        published_at = _clamp_published_at(_struct_to_dt(pub) if pub else None, fetched_at)

        article = Article(
            feed_id=feed.id,
            guid=guid[:2048],
            guid_hash=guid_hash,
            url=article_url,
            url_normalized=_normalize_url(article_url),
            title=(entry.get("title") or "Untitled")[:1000],
            author=_extract_author(entry),
            content=content,
            content_source=content_source,
            readable_status="skipped",
            published_at=published_at,
            word_count=word_count,
            estimated_read_min=estimated_read_min,
            image_url=_extract_image(entry),
        )
        db.add(article)
        new_articles.append(article)

    if new_articles:
        # Flush to get IDs, then apply filters before the outer commit.
        # A concurrent fetch may have inserted the same article between our SELECT and now;
        # the IntegrityError propagates to fetch_feed's dedicated handler, which treats
        # this benign race as "0 new articles" rather than a fetch failure.
        await db.flush()

        from app.services.filter_service import apply_filters_to_new_articles
        await apply_filters_to_new_articles(feed.id, new_articles, db)

        await _dedup_cross_feed(feed.id, new_articles, db)

        # Auto-detect full-content feed and disable readable extraction if warranted
        from app.services.readable_service import maybe_disable_readable_for_feed
        await maybe_disable_readable_for_feed(feed.id, db)

    return len(new_articles)


async def dedup_cross_feed_global(since: datetime, db: AsyncSession) -> int:
    """Post-gather dedup: marks cross-feed duplicate articles as read.

    Catches race conditions where _dedup_cross_feed (per-feed, pre-commit) couldn't see
    the other feed's uncommitted articles. Scoped to articles fetched since `since`.
    Returns number of (user, article) pairs marked as read.

    Only marks the higher-ID article (newer) as read, keeping the lowest-ID one unread.
    This prevents the race-condition case where both duplicates get marked as read.
    """
    ArticleB = aliased(Article)
    UserFeedB = aliased(UserFeed)

    dup_exists = (
        select(literal(1))
        .select_from(ArticleB)
        .join(UserFeedB, UserFeedB.feed_id == ArticleB.feed_id)
        .where(
            UserFeedB.user_id == UserFeed.user_id,
            ArticleB.url_normalized == Article.url_normalized,
            ArticleB.id < Article.id,
        )
        .correlate(UserFeed, Article)
        .exists()
    )

    rows = (await db.execute(
        select(UserFeed.user_id, Article.id.label("article_id"))
        .select_from(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(
            Article.url_normalized.is_not(None),
            Article.fetched_at >= since,
            dup_exists,
        )
    )).all()

    if not rows:
        return 0

    await db.execute(
        pg_insert(UserArticleState)
        .values([{"user_id": r.user_id, "article_id": r.article_id, "is_read": True} for r in rows])
        .on_conflict_do_nothing()
    )

    await db.commit()
    return len(rows)


async def _dedup_cross_feed(
    feed_id: int, new_articles: list[Article], db: AsyncSession
) -> None:
    """For users subscribed to this feed who already have the same URL from another feed,
    mark the new duplicate article as read."""
    articles_with_url = [a for a in new_articles if a.url_normalized]
    if not articles_with_url:
        return

    ArticleB = aliased(Article)
    UserFeedB = aliased(UserFeed)

    dup_exists = (
        select(literal(1))
        .select_from(ArticleB)
        .join(UserFeedB, UserFeedB.feed_id == ArticleB.feed_id)
        .where(
            UserFeedB.user_id == UserFeed.user_id,
            ArticleB.url_normalized == Article.url_normalized,
            ArticleB.id != Article.id,
        )
        .correlate(UserFeed, Article)
        .exists()
    )

    rows = (await db.execute(
        select(UserFeed.user_id, Article.id.label("article_id"))
        .select_from(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(
            Article.id.in_([a.id for a in articles_with_url]),
            Article.url_normalized.is_not(None),
            dup_exists,
        )
    )).all()

    if not rows:
        return

    await db.execute(
        pg_insert(UserArticleState)
        .values([{"user_id": r.user_id, "article_id": r.article_id, "is_read": True} for r in rows])
        .on_conflict_do_nothing()
    )


# ── helpers ──────────────────────────────────────────────────────────────────

def _extract_content(entry) -> tuple[str | None, str | None]:
    if entry.get("content"):
        for c in entry.content:
            if c.get("value"):
                return c.value, "feed_full"
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


# Platforms whose feeds always carry the complete post body, so readable extraction
# adds nothing and often makes things worse (Tumblr renders the post twice on the page
# plus a large likes/reblogs "notes" list). Matched case-insensitively against the feed's
# <generator>. These are microblogging platforms whose posts are usually well under the
# word-count threshold below, so the length heuristic alone never catches them.
_FULL_CONTENT_GENERATORS = ("tumblr",)


def _is_known_full_content_platform(parsed: feedparser.FeedParserDict) -> bool:
    generator = (parsed.feed.get("generator") or "").lower()
    return any(g in generator for g in _FULL_CONTENT_GENERATORS)


def is_full_content_feed(parsed: feedparser.FeedParserDict, sample: int = 5, threshold: int = 500) -> bool:
    """
    Heuristic: return True if the feed appears to deliver full article content.
    Known full-content platforms (by <generator>) short-circuit to True; otherwise
    checks up to `sample` entries and returns True if the majority exceed `threshold` words.
    """
    if _is_known_full_content_platform(parsed):
        return True
    counts = []
    for entry in parsed.entries[:sample]:
        content, _ = _extract_content(entry)
        word_count, _ = _reading_stats(content)
        if word_count is not None:
            counts.append(word_count)
    if not counts:
        return False
    return sum(1 for c in counts if c > threshold) / len(counts) >= 0.8


def _reading_stats(content: str | None) -> tuple[int | None, int | None]:
    if not content:
        return None, None
    plain = nh3.clean(content, tags=set())
    words = len(re.findall(r"\w+", plain))
    return words, max(1, round(words / 200))


def _url_dedup_keys(urls) -> set[str]:
    """URLs usable as a secondary dedup key: those identifying exactly one item in
    the batch.

    A link shared by several items is a section/show-level URL (e.g. podcast
    episodes all linking to the show page), not an article identifier. Deduping on
    such a URL would drop every new item whose link already exists in the DB even
    though its GUID is unique — so shared URLs are excluded here. Falsy URLs are
    ignored.
    """
    counts = Counter(u for u in urls if u)
    return {u for u, n in counts.items() if n == 1}


def _struct_to_dt(t) -> datetime:
    return datetime(*t[:6], tzinfo=timezone.utc)


_PUBLISHED_MIN = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _clamp_published_at(dt: datetime | None, fetched_at: datetime) -> datetime | None:
    """Return None if dt is implausibly old or far in the future."""
    if dt is None:
        return None
    if dt < _PUBLISHED_MIN or dt > fetched_at + timedelta(days=1):
        return None
    return dt


def _latest_published(entries) -> datetime | None:
    dates = []
    for e in entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        if t:
            dates.append(_struct_to_dt(t))
    return max(dates) if dates else None


def _normalize_guid(raw: str) -> str:
    """Strip URL fragment from GUIDs that are HTTP URLs.

    Some feeds (e.g. BBC) append a changing fragment (#0, #2, …) to article URLs
    used as GUIDs, causing the same article to be stored multiple times.
    Non-URL GUIDs (UUIDs, opaque strings) are returned unchanged.
    """
    try:
        p = urlparse(raw)
        if p.scheme in ("http", "https"):
            return urlunparse(p._replace(fragment=""))
    except Exception:
        pass
    return raw


def _safe_url(value: str | None, max_len: int = 2048) -> str | None:
    """Allow only http/https URLs to prevent javascript: and other dangerous schemes."""
    if not value:
        return None
    stripped = value.strip()
    if not stripped.lower().startswith(("http://", "https://")):
        return None
    return stripped[:max_len]

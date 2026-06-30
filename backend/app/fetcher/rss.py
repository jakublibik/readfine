"""RSS feed fetcher: HTTP fetch + feedparser + article deduplication."""
import asyncio
import hashlib
import logging
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import httpx
import nh3
from sqlalchemy import case, func, literal, select, update
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
    TRANSIENT_HTTP_STATUSES,
    async_validate_feed_url,
    fetch_url_conditional,
    fetch_url_with_ssrf_check,
    parse_retry_after,
    redact_url,
    validate_feed_url,
)

logger = logging.getLogger(__name__)

FETCH_ERROR_DISABLE_THRESHOLD = 5  # consecutive failures before feed is disabled

_HEADERS = {
    "User-Agent": READFINE_UA,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}
_TIMEOUT = 30  # seconds
_MAX_REDIRECTS = 5

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



async def fetch_and_parse_url(url: str) -> feedparser.FeedParserDict:
    """Fetch a URL and parse it as RSS/Atom. Raises on HTTP or parse failure."""
    await async_validate_feed_url(url)
    loop = asyncio.get_running_loop()
    content = await loop.run_in_executor(
        None, fetch_url_with_ssrf_check, url, None, _TIMEOUT, _HEADERS
    )
    parsed = await loop.run_in_executor(None, feedparser.parse, content)

    if parsed.bozo:
        import xml.sax._exceptions as _sax
        if isinstance(parsed.bozo_exception, _sax.SAXParseException):
            # XML parse error means the response is HTML, not RSS
            raise ValueError(f"Not a valid RSS/Atom feed: {parsed.bozo_exception}")
        if not parsed.entries and not parsed.feed:
            raise ValueError(f"Not a valid RSS/Atom feed: {parsed.bozo_exception}")

    return parsed


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
                feed.retry_after_until = None
                await db.commit()
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
        http_status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
        # 429/408 are transient (rate limit / timeout): back off via the normal
        # error tier instead of disabling on first hit. Other 4xx stay permanent.
        is_permanent_4xx = (
            http_status is not None
            and 400 <= http_status < 500
            and http_status not in TRANSIENT_HTTP_STATUSES
        )
        db.add(FetchLog(
            feed_id=feed_id,
            failed_at=datetime.now(timezone.utc),
            http_status=http_status,
            error_message=str(exc)[:500],
        ))
        if is_permanent_4xx:
            new_status = literal("disabled")
        else:
            new_status = case(
                (Feed.fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD, literal("disabled")),
                else_=literal("error"),
            )
        now = datetime.now(timezone.utc)
        retry_after_until = None
        if http_status in TRANSIENT_HTTP_STATUSES and isinstance(exc, httpx.HTTPStatusError):
            retry_after_until = parse_retry_after(exc.response.headers.get("retry-after"), now)
        await db.execute(
            update(Feed).where(Feed.id == feed_id).values(
                status=new_status,
                fetch_error_count=Feed.fetch_error_count + 1,
                last_error=str(exc)[:500],
                last_fetched_at=now,
                retry_after_until=retry_after_until,
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

        # Increment unread_count for every subscriber of this feed.
        # Filters may decrement it afterwards for articles they mark as read.
        await db.execute(
            update(UserFeed)
            .where(UserFeed.feed_id == feed.id)
            .values(unread_count=UserFeed.unread_count + len(new_articles))
        )

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
        select(UserFeed.user_id, Article.id.label("article_id"), Article.feed_id)
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

    by_feed: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        by_feed[r.feed_id].append(r.user_id)

    for feed_id, user_ids in by_feed.items():
        unread_subq = (
            select(func.count())
            .select_from(Article)
            .outerjoin(
                UserArticleState,
                (UserArticleState.article_id == Article.id)
                & (UserArticleState.user_id == UserFeed.user_id),
            )
            .where(
                Article.feed_id == feed_id,
                (UserArticleState.is_read == False) | UserArticleState.is_read.is_(None),
            )
            .correlate(UserFeed)
            .scalar_subquery()
        )
        await db.execute(
            update(UserFeed)
            .where(UserFeed.user_id.in_(user_ids), UserFeed.feed_id == feed_id)
            .values(unread_count=unread_subq)
        )

    await db.commit()
    return len(rows)


async def _dedup_cross_feed(
    feed_id: int, new_articles: list[Article], db: AsyncSession
) -> None:
    """For users subscribed to this feed who already have the same URL from another feed,
    mark the new duplicate article as read and adjust unread_count."""
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

    affected_user_ids = list({r.user_id for r in rows})
    unread_subq = (
        select(func.count())
        .select_from(Article)
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id)
            & (UserArticleState.user_id == UserFeed.user_id),
        )
        .where(
            Article.feed_id == feed_id,
            (UserArticleState.is_read == False) | UserArticleState.is_read.is_(None),
        )
        .correlate(UserFeed)
        .scalar_subquery()
    )
    await db.execute(
        update(UserFeed)
        .where(UserFeed.user_id.in_(affected_user_ids), UserFeed.feed_id == feed_id)
        .values(unread_count=unread_subq)
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


def is_full_content_feed(parsed: feedparser.FeedParserDict, sample: int = 5, threshold: int = 500) -> bool:
    """
    Heuristic: return True if the feed appears to deliver full article content.
    Checks up to `sample` entries; returns True if the majority exceed `threshold` words.
    """
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

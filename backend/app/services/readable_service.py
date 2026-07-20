"""Readable extraction pipeline: trafilatura → readability-lxml fallback."""
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
import nh3
import trafilatura
from bs4 import BeautifulSoup
from readability import Document
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.feed import Feed, UserFeed
from app.utils.http_client import READFINE_UA

logger = logging.getLogger(__name__)

# Extraction settings
_TIMEOUT = 15  # seconds per HTTP request
_MAX_RETRIES = 3
_BACKOFF_MINUTES = [5, 30, 120]  # retry delays after 1st, 2nd, 3rd failure
_BATCH_SIZE = 20  # articles processed per scheduler run
_MAX_REDIRECTS = 5  # maximum followed redirects per request

# Auto-disable threshold: if this fraction of sampled articles have word_count > 500,
# the feed is considered full-content and readable extraction is disabled.
_FULL_CONTENT_THRESHOLD = 0.8
_FULL_CONTENT_SAMPLE = 10  # how many recent articles to sample

# Error message used when extraction yields no usable content (page produced nothing,
# or the result collapsed to whitespace after sanitization, e.g. Reddit comment pages).
_EMPTY_CONTENT_MSG = "No content could be extracted from the page"


# ── core extraction ───────────────────────────────────────────────────────────

def _fetch_html(url: str, auth_user: Optional[str], auth_pass: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Download article HTML. Returns (html, error_message, http_status_code)."""
    from app.utils.url_validator import log_outbound, validate_feed_url
    try:
        validate_feed_url(url)
    except ValueError as exc:
        logger.warning("readable URL blocked (SSRF): %s — %s", url, exc)
        return None, str(exc), None

    try:
        auth = (auth_user, auth_pass) if auth_user and auth_pass else None
        headers = {"User-Agent": READFINE_UA}
        current_url = url
        # http2=True: some CDNs 403 / hard-throttle HTTP/1.1 as a bot signal but serve
        # HTTP/2 normally (see url_validator._resolve_response).
        with httpx.Client(
            timeout=_TIMEOUT, follow_redirects=False, auth=auth, headers=headers, http2=True
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                started = time.monotonic()
                try:
                    resp = client.get(current_url)
                except Exception as exc:
                    log_outbound(current_url, None, started, error=type(exc).__name__)
                    raise
                # Extraction has its own client (no host throttling), so it must be
                # visible in the outbound log too — otherwise its share of a host's
                # rate-limit budget is invisible.
                log_outbound(current_url, resp, started)
                if not resp.is_redirect:
                    break
                redirect_url = resp.headers.get("location", "")
                # Resolve relative redirects against the current URL
                if redirect_url and not redirect_url.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    redirect_url = urljoin(current_url, redirect_url)
                try:
                    validate_feed_url(redirect_url)
                except ValueError as exc:
                    logger.warning("readable redirect blocked (SSRF): %s — %s", redirect_url, exc)
                    return None, f"Redirect blocked: {exc}", None
                current_url = redirect_url
            else:
                return None, f"Too many redirects (max {_MAX_REDIRECTS})", None

        resp.raise_for_status()
        return resp.text, None, None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        msg = f"HTTP {status_code} {exc.response.reason_phrase}"
        logger.warning("readable fetch failed for %s: %s", url, msg)
        return None, msg, status_code
    except httpx.TimeoutException:
        msg = f"Timeout after {_TIMEOUT}s"
        logger.warning("readable fetch timed out for %s", url)
        return None, msg, None
    except Exception as exc:
        msg = str(exc)[:200]
        logger.warning("readable fetch failed for %s: %s", url, msg)
        return None, msg, None


def _strip_pre_extraction_noise(html: str) -> str:
    """Remove page chrome that extractors mistake for the main content.

    Tumblr renders a large `<ol class="notes">` list of likes/reblogs (inside
    `#notecontainer`). It is the biggest block on a short post, so trafilatura's
    precision heuristics latch onto it and return the notes list *instead* of the
    post body — and because that result is non-empty, the readability fallback never
    runs. Dropping it before extraction lets the real post surface. We also drop
    `<noscript>` here: Tumblr posts carry a tracking-pixel `<noscript>` that
    readability keeps as escaped `<img>` text, which renders as garbage in the body.
    """
    if "notecontainer" not in html and 'class="notes"' not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("#notecontainer, ol.notes"):
        el.decompose()
    for el in soup.find_all("noscript"):
        el.decompose()
    return str(soup)


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    import re
    result = trafilatura.extract(html, url=url, output_format="html",
                                 include_comments=False, include_tables=True,
                                 include_links=True, include_images=True,
                                 favor_precision=True)
    if not result:
        return None
    # trafilatura outputs <graphic src="..." alt="..."/> instead of <img>
    result = re.sub(r'<graphic\b([^>]*)/>', r'<img\1>', result)
    return result


def _collect_video_figures(html: str) -> list[str]:
    """
    Find YouTube/Vimeo iframes in raw HTML and return replacement <figure> strings.
    Trafilatura drops iframes, so we collect replacements before extraction
    and append them to the final content.
    """
    import re
    figures = []

    for m in re.finditer(r'<iframe\b[^>]*>.*?</iframe>', html, flags=re.DOTALL | re.IGNORECASE):
        iframe = m.group(0)
        src_m = re.search(r'\bsrc=["\']([^"\']+)["\']', iframe)
        if not src_m:
            continue
        src = src_m.group(1)

        yt = re.search(r'youtube\.com/embed/([A-Za-z0-9_-]+)', src)
        if yt:
            vid = yt.group(1)
            figures.append(
                f'<figure>'
                f'<a href="https://www.youtube.com/watch?v={vid}">'
                f'<img src="https://img.youtube.com/vi/{vid}/hqdefault.jpg" alt="Video thumbnail">'
                f'</a>'
                f'<figcaption>&#9654; Watch on YouTube</figcaption>'
                f'</figure>'
            )
            continue

        vi = re.search(r'player\.vimeo\.com/video/(\d+)', src)
        if vi:
            vid = vi.group(1)
            figures.append(
                f'<figure>'
                f'<a href="https://vimeo.com/{vid}">'
                f'<img src="https://vumbnail.com/{vid}.jpg" alt="Video thumbnail">'
                f'</a>'
                f'<figcaption>&#9654; Watch on Vimeo</figcaption>'
                f'</figure>'
            )

    return figures


def _extract_with_readability(html: str) -> Optional[str]:
    try:
        doc = Document(html)
        content = doc.summary(html_partial=True)
        return content if content and len(content.strip()) > 50 else None
    except Exception:
        return None


def _sanitize(html: str) -> str:
    allowed_tags = {
        "a", "abbr", "article", "b", "blockquote", "br", "caption", "cite", "code",
        "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em",
        "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
        "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "rp", "rt",
        "ruby", "s", "samp", "section", "small", "span", "strong", "sub", "summary",
        "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u",
        "ul", "var",
    }
    allowed_attrs = {
        "a": {"href", "title"},
        "img": {"src", "alt", "title", "width", "height"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan", "scope"},
        "col": {"span"},
        "colgroup": {"span"},
        "time": {"datetime"},
    }
    return nh3.clean(html, tags=allowed_tags, attributes=allowed_attrs,
                     link_rel="noopener noreferrer")


# Block elements left empty after sanitization (e.g. an `<li>` whose only child was a
# stripped share-button link) render as stray bullets/gaps. Media-bearing blocks are kept.
_EMPTY_BLOCK_TAGS = ["li", "p"]
_MEDIA_TAGS = ["img", "picture", "video", "audio", "iframe", "svg", "source"]


def _has_visible_content(html: str) -> bool:
    """True if sanitized HTML has real text or media — not just whitespace.

    Some pages (e.g. Reddit comment threads) extract to markup that collapses to
    pure whitespace after sanitization; such results must not be stored as success.
    """
    if not html or not html.strip():
        return False
    soup = BeautifulSoup(html, "html.parser")
    if soup.get_text(strip=True):
        return True
    return soup.find(_MEDIA_TAGS) is not None


def _dedupe_images(html: str) -> str:
    """
    Drop repeated images that point at the same file. News CMSs emit the same photo
    several times (lead + inline + responsive <picture> renditions), and trafilatura
    extracts each one, so the readable body shows the same picture two or three times
    (seen on aktualne.cz / economia). Keys on the URL filename — not the full src — so
    different upload paths of one file (…/47/48/<uuid>/foo.jpg vs …/47/37/<uuid>/foo.jpg)
    collapse too. Keeps the first occurrence.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    for img in soup.find_all("img"):
        key = urlsplit(img.get("src") or "").path.rsplit("/", 1)[-1].lower()
        if not key:
            continue
        if key in seen:
            img.decompose()
        else:
            seen.add(key)
    return str(soup)


def _drop_empty_blocks(html: str) -> str:
    """Remove block elements with no text and no media, left empty by sanitization."""
    soup = BeautifulSoup(html, "html.parser")
    # Repeat until stable: removing an inner empty block can empty its parent.
    while True:
        removed = False
        for el in soup.find_all(_EMPTY_BLOCK_TAGS):
            if el.get_text(strip=True) or el.find(_MEDIA_TAGS):
                continue
            el.decompose()
            removed = True
        if not removed:
            break
    return str(soup)


def _find_published_date(html: str, url: str) -> Optional[datetime]:
    """
    Best-effort publication date scraped from the article page via htmldate.
    Used to backfill Article.published_at for feeds whose listing carries no date
    (e.g. sites without <time datetime> in their cards). htmldate is date-granular
    — it discards time-of-day — so the result is that date at midnight UTC.
    Returns None when no date is found or it parses implausibly.
    """
    try:
        from htmldate import find_date
        # original_date=False (htmldate's default) leans on the page's own metadata
        # (JSON-LD datePublished, meta tags) for the primary article date. Do NOT use
        # original_date=True here: it favours the *oldest* date anywhere on the page,
        # which on news sites grabs a related-article link's date (seen on denik.cz:
        # a 2024/2026 mismatch) instead of the article's own publication date.
        raw = find_date(html, url=url, original_date=False)  # default format '%Y-%m-%d'
    except Exception as exc:
        logger.debug("htmldate find_date failed for %s: %s", url, exc)
        return None
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    # Reject implausible future dates (clock skew / malformed markup) that would
    # otherwise sort to the top of the reading list.
    if dt > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return dt


def apply_readable_result(
    article: Article,
    content: Optional[str],
    error: Optional[str],
    http_status: Optional[int],
    published_at: Optional[datetime] = None,
) -> bool:
    """Apply extraction result to article fields. Returns True if HTTP 403."""
    # Whitespace-only content is treated as no content: storing it would mark the
    # article "success" yet render blank, hiding the (often fuller) feed content.
    if content and content.strip():
        article.readable_content = content
        article.readable_status = "success"
        article.readable_error = None
        plain = nh3.clean(content, tags=set())
        words = len(re.findall(r"\w+", plain))
        article.word_count = words
        article.estimated_read_min = max(1, round(words / 200))
        # Backfill the publication date from the article page only when the feed
        # listing gave us none — never override a date we already trust.
        if published_at is not None and article.published_at is None:
            article.published_at = published_at
        return False

    article.readable_error = error
    is_4xx = http_status is not None and 400 <= http_status < 500
    is_403 = http_status == 403
    if is_4xx:
        article.readable_status = "failed"
        article.readable_failed_at = datetime.now(timezone.utc)
        article.readable_next_retry_at = None
    else:
        retries = (article.readable_retries or 0) + 1
        article.readable_retries = retries
        if retries >= _MAX_RETRIES:
            article.readable_status = "failed"
            article.readable_failed_at = datetime.now(timezone.utc)
            article.readable_next_retry_at = None
        else:
            delay_min = _BACKOFF_MINUTES[min(retries - 1, len(_BACKOFF_MINUTES) - 1)]
            article.readable_next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
    return is_403


def extract_readable(url: str, auth_user: Optional[str] = None,
                     auth_pass: Optional[str] = None) -> tuple[Optional[str], Optional[str], Optional[int], Optional[datetime]]:
    """
    Download URL and extract readable HTML.
    Returns (sanitized HTML, error_message, http_status_code, published_at). On success,
    the first element is set and published_at may carry a date scraped from the page.
    """
    html, fetch_error, http_status = _fetch_html(url, auth_user, auth_pass)
    if not html:
        return None, fetch_error, http_status, None

    video_figures = _collect_video_figures(html)
    html = _strip_pre_extraction_noise(html)
    content = _extract_with_trafilatura(html, url)
    if not content:
        content = _extract_with_readability(html)
    if not content:
        logger.warning("readable extraction yielded no content for %s", url)
        return None, _EMPTY_CONTENT_MSG, None, None

    if video_figures:
        content += "\n" + "\n".join(video_figures)
    from app.utils.parsing import rewrite_relative_urls
    final = rewrite_relative_urls(_drop_empty_blocks(_dedupe_images(_sanitize(content))), url)
    if not _has_visible_content(final):
        # Extraction produced markup that sanitized down to nothing usable.
        logger.warning("readable extraction collapsed to empty content for %s", url)
        return None, _EMPTY_CONTENT_MSG, None, None
    return final, None, None, _find_published_date(html, url)


# ── scheduler job ─────────────────────────────────────────────────────────────

async def process_pending_readable(db: AsyncSession) -> int:
    """
    Process a batch of articles with readable_status='pending'.
    Returns the number of articles processed.
    """
    result = await db.execute(
        select(Article)
        .where(
            Article.readable_status == "pending",
            # never re-extract a retention-trimmed stub — it would re-fetch the body
            Article.trimmed_at.is_(None),
            Article.url.isnot(None),
            Article.url != "",
            and_(
                Article.readable_next_retry_at.is_(None)
                | (Article.readable_next_retry_at <= datetime.now(timezone.utc))
            ),
        )
        .order_by(Article.id)
        .limit(_BATCH_SIZE)
    )
    articles = result.scalars().all()
    if not articles:
        return 0

    # Load feed auth info for articles that need it
    feed_ids = list({a.feed_id for a in articles})
    feeds_result = await db.execute(
        select(Feed.id, Feed.fetch_auth_user, Feed.fetch_auth_pass_encrypted)
        .where(Feed.id.in_(feed_ids))
    )
    feed_auth: dict[int, tuple[Optional[str], Optional[str]]] = {}
    for feed_id, auth_user, auth_pass_enc in feeds_result:
        decrypted_pass: Optional[str] = None
        if auth_pass_enc:
            try:
                from app.utils.crypto import decrypt
                decrypted_pass = decrypt(auth_pass_enc)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt fetch_auth_pass for feed %d: %s", feed_id, exc
                )
        feed_auth[feed_id] = (auth_user, decrypted_pass)

    import asyncio
    loop = asyncio.get_running_loop()

    processed = 0
    feed_403_streak: dict[int, int] = {}     # consecutive 403s per feed in this batch
    feeds_to_disable: set[int] = set()       # feeds that hit the 403 threshold
    feeds_with_403: set[int] = set()         # feeds with any 403 (cross-batch check)
    feed_empty_streak: dict[int, int] = {}   # consecutive empty extractions per feed
    feeds_to_disable_empty: set[int] = set() # feeds that hit the empty threshold
    feeds_with_empty: set[int] = set()       # feeds with any empty (cross-batch check)

    for article in articles:
        # Feed hit a disable threshold earlier in this batch — skip without fetching.
        # Leave it 'pending' (don't mark skipped here): the disable step below cancels
        # *and* runs the AI pipeline on every still-pending article for the feed, so
        # marking it skipped now would orphan it from scoring/filters.
        if article.feed_id in feeds_to_disable or article.feed_id in feeds_to_disable_empty:
            processed += 1
            continue

        auth_user, auth_pass = feed_auth.get(article.feed_id, (None, None))
        try:
            content, error, http_status, published_at = await loop.run_in_executor(
                None, extract_readable, article.url, auth_user, auth_pass
            )
        except Exception as exc:
            content, error, http_status, published_at = None, str(exc)[:200], None, None
            logger.warning("readable extraction error for article %d: %s", article.id, exc)

        # Re-check status — on-demand extraction may have already processed this article
        await db.refresh(article)
        if article.readable_status == "success":
            processed += 1
            continue

        is_403 = apply_readable_result(article, content, error, http_status, published_at)
        is_empty = content is None and error == _EMPTY_CONTENT_MSG
        from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
        if content:
            feed_403_streak.pop(article.feed_id, None)  # reset streaks on success
            feed_empty_streak.pop(article.feed_id, None)
            await run_pipeline_for_article_all_users(article, db)
        elif article.readable_status == "failed":
            # Terminal failure — score with RSS content
            await run_pipeline_for_article_all_users(article, db)
        if is_403:
            streak = feed_403_streak.get(article.feed_id, 0) + 1
            feed_403_streak[article.feed_id] = streak
            feeds_with_403.add(article.feed_id)
            if streak >= _CONSECUTIVE_403_THRESHOLD:
                feeds_to_disable.add(article.feed_id)

        if is_empty:
            streak = feed_empty_streak.get(article.feed_id, 0) + 1
            feed_empty_streak[article.feed_id] = streak
            feeds_with_empty.add(article.feed_id)
            if streak >= _CONSECUTIVE_EMPTY_THRESHOLD:
                feeds_to_disable_empty.add(article.feed_id)
        else:
            # Any non-empty outcome breaks the consecutive-empty streak
            feed_empty_streak.pop(article.feed_id, None)

        processed += 1
        await db.commit()  # per-article: keeps transactions short even with inline AI calls

    # Feeds that hit a threshold within this batch — disable immediately
    for feed_id in feeds_to_disable:
        await _disable_readable_for_403(feed_id, db)
    for feed_id in feeds_to_disable_empty - feeds_to_disable:
        await _disable_readable_for_empty(feed_id, db)

    # Feeds below the in-batch threshold — check cross-batch consecutive counts
    for feed_id in feeds_with_403 - feeds_to_disable:
        await _maybe_disable_readable_for_403(feed_id, db)
    for feed_id in feeds_with_empty - feeds_to_disable_empty - feeds_to_disable:
        await _maybe_disable_readable_for_empty(feed_id, db)

    logger.info("readable: processed %d articles", processed)
    return processed


# ── auto-detection of full-content feeds ─────────────────────────────────────

async def maybe_disable_readable_for_feed(feed_id: int, db: AsyncSession) -> bool:
    """
    Check if a feed consistently delivers full content (word_count > 500).
    If so, disable extract_readable on all UserFeed rows for this feed.
    Returns True if disabled.
    """
    result = await db.execute(
        select(Article.word_count)
        .where(
            Article.feed_id == feed_id,
            Article.word_count.isnot(None),
        )
        .order_by(Article.id.desc())
        .limit(_FULL_CONTENT_SAMPLE)
    )
    counts = [row[0] for row in result]
    if len(counts) < _FULL_CONTENT_SAMPLE:
        return False  # not enough data yet

    full_content = sum(1 for c in counts if c > 500)
    if full_content / len(counts) < _FULL_CONTENT_THRESHOLD:
        return False

    # Disable for all subscribers
    user_feeds_result = await db.execute(
        select(UserFeed).where(
            UserFeed.feed_id == feed_id,
            UserFeed.extract_readable == True,
        )
    )
    user_feeds = user_feeds_result.scalars().all()
    if not user_feeds:
        return False

    for uf in user_feeds:
        uf.extract_readable = False
        uf.readable_auto_disabled = True
        uf.readable_auto_disabled_reason = "full_content"
    await db.commit()

    # Mark pending articles for this feed as skipped (no need to extract)
    pending_result = await db.execute(
        select(Article).where(
            Article.feed_id == feed_id,
            Article.readable_status == "pending",
        )
    )
    pending = pending_result.scalars().all()
    for article in pending:
        article.readable_status = "skipped"
    if pending:
        await db.commit()

    logger.info(
        "readable: auto-disabled extraction for feed %d (%d/%d articles have full content)",
        feed_id, full_content, len(counts),
    )
    return True


_CONSECUTIVE_403_THRESHOLD = 3
# Empty extractions are a weaker signal than 403s (an odd article can extract to
# nothing on an otherwise-good feed), so require a longer streak before disabling.
_CONSECUTIVE_EMPTY_THRESHOLD = 5


async def _disable_readable_for_feed(
    feed_id: int, db: AsyncSession, *, pending_error: str
) -> Optional[int]:
    """Turn off readable extraction for a feed and cancel its pending articles.

    Returns the number of pending articles cancelled, or None if the feed had no
    active subscribers to disable (nothing happened).
    """
    user_feeds_result = await db.execute(
        select(UserFeed).where(
            UserFeed.feed_id == feed_id,
            UserFeed.extract_readable == True,
        )
    )
    user_feeds = user_feeds_result.scalars().all()
    if not user_feeds:
        return None

    for uf in user_feeds:
        uf.extract_readable = False
        uf.readable_auto_disabled = True
        uf.readable_auto_disabled_reason = "blocked"

    pending_result = await db.execute(
        select(Article).where(
            Article.feed_id == feed_id,
            Article.readable_status == "pending",
        )
    )
    pending = pending_result.scalars().all()
    for article in pending:
        article.readable_status = "skipped"
        article.readable_error = pending_error

    await db.commit()

    from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
    for article in pending:
        await run_pipeline_for_article_all_users(article, db)
    return len(pending)


async def _disable_readable_for_403(feed_id: int, db: AsyncSession) -> None:
    """Disable readable extraction for a feed after repeated 403 responses."""
    cancelled = await _disable_readable_for_feed(
        feed_id, db, pending_error="HTTP 403 Forbidden"
    )
    if cancelled is None:
        return
    logger.warning(
        "readable: disabled extraction for feed %d after %d consecutive 403 errors"
        " (cancelled %d pending articles)",
        feed_id, _CONSECUTIVE_403_THRESHOLD, cancelled,
    )


async def _disable_readable_for_empty(feed_id: int, db: AsyncSession) -> None:
    """Disable readable extraction for a feed that consistently extracts nothing."""
    cancelled = await _disable_readable_for_feed(
        feed_id, db, pending_error=_EMPTY_CONTENT_MSG
    )
    if cancelled is None:
        return
    logger.warning(
        "readable: disabled extraction for feed %d after %d consecutive empty"
        " extractions (cancelled %d pending articles)",
        feed_id, _CONSECUTIVE_EMPTY_THRESHOLD, cancelled,
    )


async def _maybe_disable_readable_for_403(feed_id: int, db: AsyncSession) -> None:
    """Disable readable if the last N processed articles for the feed all returned 403.

    Used for cross-batch detection: when a feed accumulates 403s across multiple scheduler
    runs (few articles per batch), this catches it once the consecutive count is reached.
    """
    result = await db.execute(
        select(Article.readable_status, Article.readable_error)
        .where(
            Article.feed_id == feed_id,
            Article.readable_status.in_(["failed", "success"]),
        )
        .order_by(Article.id.desc())
        .limit(_CONSECUTIVE_403_THRESHOLD)
    )
    rows = result.all()

    if len(rows) < _CONSECUTIVE_403_THRESHOLD:
        return

    all_403 = all(
        status == "failed" and error and " 403" in error
        for status, error in rows
    )
    if not all_403:
        return

    await _disable_readable_for_403(feed_id, db)


async def _maybe_disable_readable_for_empty(feed_id: int, db: AsyncSession) -> None:
    """Disable readable if the last N terminal articles for the feed all extracted empty.

    Cross-batch counterpart of the in-batch streak check: when empty extractions
    accumulate across scheduler runs (few articles per batch), this catches the feed
    once enough terminally-failed articles share the empty-content error.
    """
    result = await db.execute(
        select(Article.readable_status, Article.readable_error)
        .where(
            Article.feed_id == feed_id,
            Article.readable_status.in_(["failed", "success"]),
        )
        .order_by(Article.id.desc())
        .limit(_CONSECUTIVE_EMPTY_THRESHOLD)
    )
    rows = result.all()

    if len(rows) < _CONSECUTIVE_EMPTY_THRESHOLD:
        return

    all_empty = all(
        status == "failed" and error == _EMPTY_CONTENT_MSG
        for status, error in rows
    )
    if not all_empty:
        return

    await _disable_readable_for_empty(feed_id, db)

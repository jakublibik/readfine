"""Readable extraction pipeline: trafilatura → readability-lxml fallback."""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import nh3
import trafilatura
from readability import Document
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.feed import Feed, UserFeed

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


# ── core extraction ───────────────────────────────────────────────────────────

def _fetch_html(url: str, auth_user: Optional[str], auth_pass: Optional[str]) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """Download article HTML. Returns (html, error_message, http_status_code)."""
    from app.utils.url_validator import validate_feed_url
    try:
        validate_feed_url(url)
    except ValueError as exc:
        logger.warning("readable URL blocked (SSRF): %s — %s", url, exc)
        return None, str(exc), None

    try:
        auth = (auth_user, auth_pass) if auth_user and auth_pass else None
        headers = {"User-Agent": "Readfine/1.0 (+https://github.com/readfine)"}
        current_url = url
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False, auth=auth, headers=headers) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                resp = client.get(current_url)
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


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    import re
    result = trafilatura.extract(html, url=url, output_format="html",
                                 include_comments=False, include_tables=True,
                                 include_links=True, include_images=True,
                                 favor_recall=True)
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


def apply_readable_result(
    article: Article,
    content: Optional[str],
    error: Optional[str],
    http_status: Optional[int],
) -> bool:
    """Apply extraction result to article fields. Returns True if HTTP 403."""
    if content:
        article.readable_content = content
        article.readable_status = "success"
        article.readable_error = None
        plain = nh3.clean(content, tags=set())
        words = len(re.findall(r"\w+", plain))
        article.word_count = words
        article.estimated_read_min = max(1, round(words / 200))
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
                     auth_pass: Optional[str] = None) -> tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Download URL and extract readable HTML.
    Returns (sanitized HTML, error_message, http_status_code). On success, only the first element is set.
    """
    html, fetch_error, http_status = _fetch_html(url, auth_user, auth_pass)
    if not html:
        return None, fetch_error, http_status

    video_figures = _collect_video_figures(html)
    content = _extract_with_trafilatura(html, url)
    if not content:
        content = _extract_with_readability(html)
    if not content:
        msg = "No content could be extracted from the page"
        logger.warning("readable extraction yielded no content for %s", url)
        return None, msg, None

    if video_figures:
        content += "\n" + "\n".join(video_figures)
    from app.utils.parsing import rewrite_relative_urls
    return rewrite_relative_urls(_sanitize(content), url), None, None


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
    feed_403_streak: dict[int, int] = {}   # consecutive 403s per feed in this batch
    feeds_to_disable: set[int] = set()     # feeds that already hit the threshold
    feeds_with_403: set[int] = set()       # feeds with any 403 (cross-batch check)

    for article in articles:
        # Feed hit threshold earlier in this batch — skip without fetching
        if article.feed_id in feeds_to_disable:
            article.readable_status = "skipped"
            processed += 1
            continue

        auth_user, auth_pass = feed_auth.get(article.feed_id, (None, None))
        try:
            content, error, http_status = await loop.run_in_executor(
                None, extract_readable, article.url, auth_user, auth_pass
            )
        except Exception as exc:
            content, error, http_status = None, str(exc)[:200], None
            logger.warning("readable extraction error for article %d: %s", article.id, exc)

        # Re-check status — on-demand extraction may have already processed this article
        await db.refresh(article)
        if article.readable_status == "success":
            processed += 1
            continue

        is_403 = apply_readable_result(article, content, error, http_status)
        if content:
            feed_403_streak.pop(article.feed_id, None)  # reset streak on success
        elif is_403:
            streak = feed_403_streak.get(article.feed_id, 0) + 1
            feed_403_streak[article.feed_id] = streak
            feeds_with_403.add(article.feed_id)
            if streak >= _CONSECUTIVE_403_THRESHOLD:
                feeds_to_disable.add(article.feed_id)

        processed += 1

    await db.commit()

    # Feeds that hit the threshold within this batch — disable immediately
    for feed_id in feeds_to_disable:
        await _disable_readable_for_403(feed_id, db)

    # Feeds with some 403s but not yet at threshold — check cross-batch consecutive count
    for feed_id in feeds_with_403 - feeds_to_disable:
        await _maybe_disable_readable_for_403(feed_id, db)

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


async def _disable_readable_for_403(feed_id: int, db: AsyncSession) -> None:
    """Disable readable extraction for a feed and cancel all pending articles."""
    user_feeds_result = await db.execute(
        select(UserFeed).where(
            UserFeed.feed_id == feed_id,
            UserFeed.extract_readable == True,
        )
    )
    user_feeds = user_feeds_result.scalars().all()
    if not user_feeds:
        return

    for uf in user_feeds:
        uf.extract_readable = False

    pending_result = await db.execute(
        select(Article).where(
            Article.feed_id == feed_id,
            Article.readable_status == "pending",
        )
    )
    pending = pending_result.scalars().all()
    for article in pending:
        article.readable_status = "skipped"
        article.readable_error = "HTTP 403 Forbidden"

    await db.commit()
    logger.warning(
        "readable: disabled extraction for feed %d after %d consecutive 403 errors"
        " (cancelled %d pending articles)",
        feed_id, _CONSECUTIVE_403_THRESHOLD, len(pending),
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

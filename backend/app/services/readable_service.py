"""Readable extraction pipeline: trafilatura → readability-lxml fallback."""
import logging
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

# Auto-disable threshold: if this fraction of sampled articles have word_count > 500,
# the feed is considered full-content and readable extraction is disabled.
_FULL_CONTENT_THRESHOLD = 0.8
_FULL_CONTENT_SAMPLE = 10  # how many recent articles to sample


# ── core extraction ───────────────────────────────────────────────────────────

def _fetch_html(url: str, auth_user: Optional[str], auth_pass: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Download article HTML. Returns (html, error_message)."""
    try:
        auth = (auth_user, auth_pass) if auth_user and auth_pass else None
        resp = httpx.get(url, timeout=_TIMEOUT, follow_redirects=True, auth=auth,
                         headers={"User-Agent": "Filtread/1.0 (+https://github.com/filtread)"})
        resp.raise_for_status()
        return resp.text, None
    except httpx.HTTPStatusError as exc:
        msg = f"HTTP {exc.response.status_code} {exc.response.reason_phrase}"
        logger.warning("readable fetch failed for %s: %s", url, msg)
        return None, msg
    except httpx.TimeoutException:
        msg = f"Timeout after {_TIMEOUT}s"
        logger.warning("readable fetch timed out for %s", url)
        return None, msg
    except Exception as exc:
        msg = str(exc)[:200]
        logger.warning("readable fetch failed for %s: %s", url, msg)
        return None, msg


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    result = trafilatura.extract(html, url=url, output_format="html",
                                 include_comments=False, include_tables=True,
                                 no_fallback=False)
    return result or None


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


def extract_readable(url: str, auth_user: Optional[str] = None,
                     auth_pass: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
    """
    Download URL and extract readable HTML.
    Returns (sanitized HTML, error_message). One of the two is always None.
    """
    html, fetch_error = _fetch_html(url, auth_user, auth_pass)
    if not html:
        return None, fetch_error

    content = _extract_with_trafilatura(html, url)
    if not content:
        content = _extract_with_readability(html)
    if not content:
        msg = "No content could be extracted from the page"
        logger.warning("readable extraction yielded no content for %s", url)
        return None, msg

    return _sanitize(content), None


# ── scheduler job ─────────────────────────────────────────────────────────────

async def process_pending_readable(db: AsyncSession) -> int:
    """
    Process a batch of articles with readable_status='pending'.
    Returns the number of articles processed.
    """
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(Article)
        .where(
            Article.readable_status == "pending",
            Article.url.isnot(None),
            Article.url != "",
            and_(
                Article.readable_next_retry_at.is_(None)
                | (Article.readable_next_retry_at <= now)
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
            except Exception:
                pass
        feed_auth[feed_id] = (auth_user, decrypted_pass)

    processed = 0
    for article in articles:
        auth_user, auth_pass = feed_auth.get(article.feed_id, (None, None))
        try:
            content, error = extract_readable(article.url, auth_user, auth_pass)
        except Exception as exc:
            content, error = None, str(exc)[:200]
            logger.warning("readable extraction error for article %d: %s", article.id, exc)

        if content:
            article.readable_content = content
            article.readable_status = "success"
            article.readable_error = None
            article.readable_retries = (article.readable_retries or 0) + 1
        else:
            retries = (article.readable_retries or 0) + 1
            article.readable_retries = retries
            article.readable_error = error
            no_retry = error and "403" in error
            if no_retry or retries >= _MAX_RETRIES:
                article.readable_status = "failed"
                article.readable_next_retry_at = None
            else:
                delay_min = _BACKOFF_MINUTES[min(retries - 1, len(_BACKOFF_MINUTES) - 1)]
                article.readable_next_retry_at = now + timedelta(minutes=delay_min)

        processed += 1

    await db.commit()
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
            UserFeed.extract_readable == True,  # noqa: E712
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

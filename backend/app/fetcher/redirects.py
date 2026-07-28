"""Adopting a feed's new address after a permanent redirect.

A feed whose stored URL 301s walks the same redirect chain on every single poll,
forever: three requests and ~800 ms where one would do, multiplied by every
subscriber, and spending per-host rate budget on hosts that count every request.
Storing the address the host gave us turns that back into one request.
"""
import logging
from datetime import datetime, timezone
from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import Feed
from app.utils.url_validator import redact_url

logger = logging.getLogger(__name__)


class RedirectConflict(NamedTuple):
    """A feed that permanently redirects onto an address another feed already holds."""
    old_url: str
    target_url: str
    holder_id: int
    detected_at: datetime


# Feeds whose permanent redirect could not be adopted because the target URL is
# taken (keyed by the moving feed's id). Populated on every conflicting fetch and
# cleared when the feed's redirect stops conflicting, so the admin dashboard can
# show whether convergence is actually happening. In-process only, like
# _initial_fetch_in_progress: a single-process fact, empty after a restart and
# refilled within one fetch cycle. A stale entry can linger if the feed later
# stops redirecting entirely (adopt_permanent_url is not called then, so nothing
# clears it), which a restart resolves; this is diagnostics, not bookkeeping.
_redirect_conflicts: dict[int, RedirectConflict] = {}


def redirect_conflicts() -> dict[int, RedirectConflict]:
    """A copy of the current unresolved redirect-conflict registry."""
    return dict(_redirect_conflicts)


async def adopt_permanent_url(
    feed_id: int,
    old_url: str,
    new_url: str,
    db: AsyncSession,
    *,
    feed_type: str = "rss",
    selector: str | None = None,
    is_private: bool = False,
) -> bool:
    """Store *new_url* as the feed's address. Returns True when it was written.

    Two rules the callers must honor, because neither is enforceable from here:

    * **Call this only when a permanent redirect was actually observed** (i.e.
      ``permanent_url`` is not None). Nearly every fetch redirects nowhere, and an
      unconditional call would add a SELECT plus a write transaction to all of them.
    * **Call it only once the response has been processed successfully** — after
      articles were saved, or on a clean 304. The most common way a feed dies is a
      301 to the site's homepage; letting the parse fail first is what stops us from
      permanently pointing the feed at that homepage.

    A different feed may already occupy the target URL (two feeds converging on one
    address). The unique indexes that would reject the write are partial (see
    migration 0037): they cover public feeds only, keyed on ``feed_url`` alone for
    RSS and on ``(feed_url, selector)`` for scrape. The conflict is checked in that
    same shape, so a private feed is free to take a URL a public one already holds.
    On a hit the rewrite is skipped and the feed keeps walking its redirect. Skips
    are logged at debug level deliberately: the condition never resolves itself, so
    at info level it would emit a line per feed per fetch, forever.

    The whole body is best-effort: the caller invokes this *after* the fetch has
    already committed its articles and marked the feed active, so a DB error while
    adopting must never bubble out. If it did, the fetcher's own ``except`` would
    roll back, record a FetchLog error and bump ``fetch_error_count`` on a feed that
    actually fetched fine. Any failure here is swallowed and the feed simply keeps
    its old URL until a later fetch retries the adoption.
    """
    try:
        if not is_private:
            conflict = select(Feed.id).where(
                Feed.feed_url == new_url,
                Feed.id != feed_id,
                Feed.is_private == False,  # noqa: E712
            )
            if feed_type == "scrape":
                conflict = conflict.where(
                    Feed.feed_type == "scrape",
                    Feed.type_config["article_links_selector"].astext == selector,
                )
            else:
                conflict = conflict.where(Feed.feed_type != "scrape")
            other_id = await db.scalar(conflict.limit(1))
            if other_id is not None:
                logger.debug(
                    "Feed %d moved to %s but feed %d already holds it, keeping the redirect",
                    feed_id, redact_url(new_url), other_id,
                )
                _redirect_conflicts[feed_id] = RedirectConflict(
                    old_url, new_url, other_id, datetime.now(timezone.utc)
                )
                return False

        await db.execute(update(Feed).where(Feed.id == feed_id).values(feed_url=new_url))
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent fetch that took the same target URL.
        await db.rollback()
        logger.debug(
            "Feed %d could not adopt %s (taken concurrently)", feed_id, redact_url(new_url)
        )
        return False
    except Exception:
        # Best-effort: never let a DB hiccup here fail an already-successful fetch.
        await db.rollback()
        logger.warning(
            "Feed %d could not adopt %s", feed_id, redact_url(new_url), exc_info=True
        )
        return False

    # The redirect resolved cleanly this time; drop any earlier conflict record.
    _redirect_conflicts.pop(feed_id, None)
    logger.info(
        "Feed %d URL updated: %s -> %s", feed_id, redact_url(old_url), redact_url(new_url)
    )
    return True

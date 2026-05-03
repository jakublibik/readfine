"""Article purge service: delete old articles according to retention settings."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)


# ── pure helpers (testable without DB) ───────────────────────────────────────

def ids_exceeding_age(
    articles: list[tuple[int, datetime]],
    cutoff: datetime,
) -> set[int]:
    """Return IDs of articles whose fetched_at is before cutoff."""
    return {aid for aid, fetched_at in articles if fetched_at < cutoff}


def ids_exceeding_count(
    articles: list[tuple[int, int | None, datetime | None, datetime]],
    keep_count: int,
) -> set[int]:
    """
    Return IDs of articles that exceed keep_count per feed.
    articles: list of (id, feed_id, published_at, fetched_at)
    Ordering: newest first (published_at if set, else fetched_at).
    """
    by_feed: dict[int | None, list[tuple[int, datetime]]] = defaultdict(list)
    for aid, feed_id, published_at, fetched_at in articles:
        by_feed[feed_id].append((aid, published_at or fetched_at))

    excess: set[int] = set()
    for items in by_feed.values():
        items.sort(key=lambda x: x[1], reverse=True)
        for aid, _ in items[keep_count:]:
            excess.add(aid)
    return excess


# ── DB helpers ────────────────────────────────────────────────────────────────

def _protected_subquery():
    """Subquery returning article IDs starred or archived by any user."""
    return (
        select(UserArticleState.article_id)
        .where(
            (UserArticleState.is_starred == True)
            | (UserArticleState.is_archived == True)
        )
    )


# ── main purge job ────────────────────────────────────────────────────────────

async def purge_old_articles(db: AsyncSession) -> int:
    """
    Delete articles exceeding retention limits.

    NULL global settings mean the respective pass is disabled globally;
    per-feed overrides still apply when set.

    Pass 1 — age-based: delete articles older than effective purge_after_days.
    Pass 2 — count-based: per feed, delete articles beyond effective purge_keep_count.
              Uses a single SQL window-function query (no N+1).

    Articles starred or archived by any user are never deleted.
    Returns total number of deleted articles.
    """
    from app.models.feed import UserFeed

    result = await db.execute(
        select(AppSettings.default_purge_after_days, AppSettings.default_purge_keep_count)
        .where(AppSettings.id == 1)
    )
    row = result.one_or_none()
    # None = admin disabled this pass globally
    global_days: int | None = row[0] if row else None
    global_count: int | None = row[1] if row else None

    protected = _protected_subquery()
    now = datetime.now(timezone.utc)
    total_deleted = 0

    # ── Pass 1: age-based ─────────────────────────────────────────────────────
    # Per-feed effective_days = coalesce(feed override, global_days).
    # If both are NULL, skip the feed entirely.
    feed_days_result = await db.execute(
        select(
            UserFeed.feed_id,
            func.max(func.coalesce(UserFeed.purge_after_days, global_days)).label("effective_days"),
        )
        .group_by(UserFeed.feed_id)
    )
    age_deleted = 0
    for r in feed_days_result:
        if r.effective_days is None:
            continue  # disabled for this feed
        cutoff = now - timedelta(days=r.effective_days)
        res = await db.execute(
            delete(Article)
            .where(
                Article.feed_id == r.feed_id,
                Article.fetched_at < cutoff,
                Article.id.not_in(protected),
            )
            .returning(Article.id)
        )
        age_deleted += len(res.fetchall())

    # Orphaned articles (feed deleted) — use global default only if set
    if global_days is not None:
        res = await db.execute(
            delete(Article)
            .where(
                Article.feed_id.is_(None),
                Article.fetched_at < now - timedelta(days=global_days),
                Article.id.not_in(protected),
            )
            .returning(Article.id)
        )
        age_deleted += len(res.fetchall())

    total_deleted += age_deleted

    # ── Pass 2: count-based (single window-function query) ────────────────────
    # Per-feed effective_count = coalesce(feed override, global_count).
    # If both are NULL, skip the feed.
    feed_counts_result = await db.execute(
        select(
            UserFeed.feed_id,
            func.max(func.coalesce(UserFeed.purge_keep_count, global_count)).label("effective_count"),
        )
        .group_by(UserFeed.feed_id)
    )
    feed_counts: dict[int, int] = {
        r.feed_id: r.effective_count
        for r in feed_counts_result
        if r.effective_count is not None
    }

    count_deleted = 0
    if feed_counts:
        # Rank all articles within their feed by recency in one query
        ranked = (
            select(
                Article.id,
                Article.feed_id,
                func.row_number()
                .over(
                    partition_by=Article.feed_id,
                    order_by=func.coalesce(Article.published_at, Article.fetched_at).desc(),
                )
                .label("rn"),
            )
            .where(Article.feed_id.in_(feed_counts.keys()))
            .subquery()
        )

        # Build a CASE expression for per-feed keep counts
        from sqlalchemy import case, literal
        keep_case = case(
            {feed_id: literal(keep) for feed_id, keep in feed_counts.items()},
            value=ranked.c.feed_id,
        )
        excess_result = await db.execute(
            select(ranked.c.id).where(ranked.c.rn > keep_case)
        )
        excess_ids = [r[0] for r in excess_result]

        if excess_ids:
            res = await db.execute(
                delete(Article)
                .where(
                    Article.id.in_(excess_ids),
                    Article.id.not_in(protected),
                )
                .returning(Article.id)
            )
            count_deleted += len(res.fetchall())

    total_deleted += count_deleted
    await db.commit()
    logger.info(
        "Purge: deleted %d articles total (age: %d, count: %d)",
        total_deleted, age_deleted, count_deleted,
    )
    return total_deleted

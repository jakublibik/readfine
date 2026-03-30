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
    Articles with feed_id=None are grouped together and also pruned.
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
            (UserArticleState.is_starred == True)  # noqa: E712
            | (UserArticleState.is_archived == True)  # noqa: E712
        )
    )


async def _effective_feed_setting(
    db: AsyncSession,
    column,
    global_default: int,
) -> dict[int, int]:
    """
    Return {feed_id: effective_value} using the most conservative (max) value
    across all UserFeed rows for that feed, falling back to global_default.
    """
    from app.models.feed import UserFeed
    result = await db.execute(
        select(
            UserFeed.feed_id,
            func.max(func.coalesce(column, global_default)).label("val"),
        ).group_by(UserFeed.feed_id)
    )
    return {r.feed_id: r.val for r in result}


# ── main purge job ────────────────────────────────────────────────────────────

async def purge_old_articles(db: AsyncSession) -> int:
    """
    Delete articles exceeding retention limits.

    Pass 1 — age-based: delete articles older than purge_after_days.
    Pass 2 — count-based: per feed, delete articles beyond purge_keep_count.

    Articles starred or archived by any user are never deleted.
    Returns total number of deleted articles.
    """
    result = await db.execute(
        select(AppSettings.default_purge_after_days, AppSettings.default_purge_keep_count)
        .where(AppSettings.id == 1)
    )
    row = result.one_or_none()
    global_days = (row[0] if row else None) or 90
    global_count = (row[1] if row else None) or 500

    from app.models.feed import UserFeed
    protected = _protected_subquery()
    now = datetime.now(timezone.utc)
    total_deleted = 0

    # ── Pass 1: age-based ─────────────────────────────────────────────────────
    feed_days = await _effective_feed_setting(db, UserFeed.purge_after_days, global_days)
    age_deleted = 0

    for feed_id, days in feed_days.items():
        cutoff = now - timedelta(days=days)
        res = await db.execute(
            delete(Article)
            .where(
                Article.feed_id == feed_id,
                Article.fetched_at < cutoff,
                Article.id.not_in(protected),
            )
            .returning(Article.id)
        )
        age_deleted += len(res.fetchall())

    # Orphaned articles (feed deleted)
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

    # ── Pass 2: count-based ───────────────────────────────────────────────────
    feed_counts = await _effective_feed_setting(db, UserFeed.purge_keep_count, global_count)
    count_deleted = 0

    for feed_id, keep in feed_counts.items():
        # Fetch (id, published_at, fetched_at) for this feed
        rows = await db.execute(
            select(Article.id, Article.published_at, Article.fetched_at)
            .where(Article.feed_id == feed_id)
        )
        articles = [(r.id, feed_id, r.published_at, r.fetched_at) for r in rows]
        excess_ids = list(ids_exceeding_count(articles, keep))
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

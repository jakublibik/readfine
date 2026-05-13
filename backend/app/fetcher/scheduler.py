"""APScheduler integration: periodic RSS feed fetching and readable extraction."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, case, func, literal_column, or_, select

import app.database as db
from app.fetcher.rss import FETCH_ERROR_DISABLE_THRESHOLD, dedup_cross_feed_global, fetch_feed
from app.models.feed import Feed, UserFeed
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


async def _fetch_due_feeds() -> None:
    """Job: fetch all active feeds that are due for an update."""
    if db.async_session_factory is None:
        return

    async with db.async_session_factory() as session:
        # Resolve global settings
        result = await session.execute(
            select(
                AppSettings.default_fetch_interval_min,
                AppSettings.min_fetch_interval_min,
                AppSettings.default_purge_after_days,
            ).where(AppSettings.id == 1)
        )
        row = result.one_or_none()
        default_interval = (row[0] if row else None) or 60
        min_interval = (row[1] if row else None) or 15
        global_purge_days = (row[2] if row else None)
        now = datetime.now(timezone.utc)

        # active: fetch when due; error: tiered backoff; paused/disabled: skip
        error_backoff_min = max(15, default_interval * 2)
        one_minute = literal_column("interval '1 minute'")
        # per-feed interval clamped to global minimum
        effective_interval = func.greatest(
            func.coalesce(Feed.fetch_interval_min, default_interval),
            min_interval,
        ) * one_minute
        # count 0–(threshold-1): regular backoff; count threshold+: 24 h, then disabled
        error_backoff = case(
            (Feed.fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD, literal_column("interval '24 hours'")),
            else_=literal_column(f"interval '{error_backoff_min} minutes'"),
        )
        grace = literal_column("interval '2 minutes'")
        due_feeds = await session.execute(
            select(Feed).where(
                Feed.subscriber_count > 0,
                or_(
                    and_(
                        Feed.status == "active",
                        or_(
                            Feed.last_fetched_at.is_(None),
                            Feed.last_fetched_at + effective_interval < func.now() + grace,
                        ),
                    ),
                    and_(
                        Feed.status == "error",
                        Feed.last_fetched_at + error_backoff < func.now() + grace,
                    ),
                ),
            )
        )
        feeds = due_feeds.scalars().all()

        # Per-feed published_cutoff: MAX(COALESCE(user_feed.purge_after_days, global))
        # Matches the purge service logic so fetcher and purge stay consistent.
        cutoff_by_feed: dict[int, datetime | None] = {}
        if feeds:
            days_rows = await session.execute(
                select(
                    UserFeed.feed_id,
                    func.max(func.coalesce(UserFeed.purge_after_days, global_purge_days)).label("effective_days"),
                )
                .where(UserFeed.feed_id.in_([f.id for f in feeds]))
                .group_by(UserFeed.feed_id)
            )
            for r in days_rows:
                cutoff_by_feed[r.feed_id] = (
                    now - timedelta(days=r.effective_days) if r.effective_days is not None else None
                )

    if not feeds:
        return

    logger.info("Scheduler: %d feeds due for fetch", len(feeds))
    from app.services.feed import _initial_fetch_in_progress

    semaphore = asyncio.Semaphore(10)

    async def _fetch_one(feed_id: int) -> None:
        async with semaphore:
            if feed_id in _initial_fetch_in_progress:
                logger.debug("Scheduler: skipping feed %d — initial fetch in progress", feed_id)
                return
            async with db.async_session_factory() as session:
                feed_in_session = await session.get(Feed, feed_id)
                if feed_in_session and feed_in_session.id not in _initial_fetch_in_progress:
                    if feed_in_session.feed_type == "scrape":
                        from app.fetcher.scrape import fetch_scrape_feed
                        await fetch_scrape_feed(feed_in_session, session)
                    else:
                        await fetch_feed(
                            feed_in_session, session,
                            published_cutoff=cutoff_by_feed.get(feed_id),
                        )

    fetch_start = datetime.now(timezone.utc)
    await asyncio.gather(*[_fetch_one(feed.id) for feed in feeds], return_exceptions=True)

    # Post-gather dedup: catches race conditions where two concurrent sessions couldn't
    # see each other's uncommitted articles during per-feed _dedup_cross_feed.
    async with db.async_session_factory() as session:
        n = await dedup_cross_feed_global(fetch_start, session)
        if n:
            logger.info("Post-gather dedup: marked %d (user, article) pairs as read", n)


async def _process_readable() -> None:
    """Job: extract readable content for pending articles."""
    if db.async_session_factory is None:
        return
    from app.services.readable_service import process_pending_readable
    async with db.async_session_factory() as session:
        await process_pending_readable(session)


async def _process_ai_scoring() -> None:
    """Job: process pending AI scoring jobs."""
    if db.async_session_factory is None:
        return
    from app.services.ai_scoring_service import process_pending_scoring
    async with db.async_session_factory() as session:
        await process_pending_scoring(session)


async def _process_ai_filters() -> None:
    """Job: apply AI filters to articles that received a fresh ai_score."""
    if db.async_session_factory is None:
        return
    from app.services.filter_service import process_ai_filters_batch
    async with db.async_session_factory() as session:
        await process_ai_filters_batch(session)


async def _purge_old_articles() -> None:
    """Job: delete articles exceeding retention limits."""
    if db.async_session_factory is None:
        return
    from app.services.purge_service import purge_old_articles
    async with db.async_session_factory() as session:
        await purge_old_articles(session)


def create_scheduler() -> AsyncIOScheduler:
    """Configure and return the scheduler (not yet started)."""
    scheduler.add_job(
        _fetch_due_feeds,
        trigger="cron",
        minute="0,15,30,45",
        id="fetch_due_feeds",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _process_readable,
        trigger="interval",
        minutes=1,
        id="process_readable",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
    )
    scheduler.add_job(
        _process_ai_scoring,
        trigger="interval",
        minutes=2,
        id="process_ai_scoring",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _process_ai_filters,
        trigger="interval",
        minutes=2,
        id="process_ai_filters",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _purge_old_articles,
        trigger="cron",
        hour=3,
        minute=0,
        id="purge_old_articles",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler

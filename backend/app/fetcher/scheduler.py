"""APScheduler integration: periodic RSS feed fetching and readable extraction."""
import logging


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, func, literal_column, or_, select

import app.database as db
from app.fetcher.rss import fetch_feed
from app.models.feed import Feed
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


async def _fetch_due_feeds() -> None:
    """Job: fetch all active feeds that are due for an update."""
    if db.async_session_factory is None:
        return

    async with db.async_session_factory() as session:
        # Resolve default and minimum intervals from app_settings
        result = await session.execute(
            select(AppSettings.default_fetch_interval_min, AppSettings.min_fetch_interval_min)
            .where(AppSettings.id == 1)
        )
        row = result.one_or_none()
        default_interval = (row[0] if row else None) or 60
        min_interval = (row[1] if row else None) or 15

        # active: fetch when due; error: retry after 5× interval (min 30 min); paused: skip
        error_backoff_min = max(30, default_interval * 5)
        one_minute = literal_column("interval '1 minute'")
        # per-feed interval clamped to global minimum
        effective_interval = func.greatest(
            func.coalesce(Feed.fetch_interval_min, default_interval),
            min_interval,
        ) * one_minute
        backoff_interval = error_backoff_min * one_minute
        due_feeds = await session.execute(
            select(Feed).where(
                Feed.subscriber_count > 0,
                or_(
                    and_(
                        Feed.status == "active",
                        or_(
                            Feed.last_fetched_at.is_(None),
                            Feed.last_fetched_at + effective_interval < func.now(),
                        ),
                    ),
                    and_(
                        Feed.status == "error",
                        Feed.last_fetched_at + backoff_interval < func.now(),
                    ),
                ),
            )
        )
        feeds = due_feeds.scalars().all()

    if not feeds:
        return

    logger.info("Scheduler: %d feeds due for fetch", len(feeds))
    for feed in feeds:
        async with db.async_session_factory() as session:
            # Re-attach the feed to the new session
            feed_in_session = await session.get(Feed, feed.id)
            if feed_in_session:
                await fetch_feed(feed_in_session, session)


async def _process_readable() -> None:
    """Job: extract readable content for pending articles."""
    if db.async_session_factory is None:
        return
    from app.services.readable_service import process_pending_readable
    async with db.async_session_factory() as session:
        await process_pending_readable(session)


def create_scheduler() -> AsyncIOScheduler:
    """Configure and return the scheduler (not yet started)."""
    scheduler.add_job(
        _fetch_due_feeds,
        trigger="interval",
        minutes=1,
        id="fetch_due_feeds",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=30,
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
    return scheduler

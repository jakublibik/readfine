"""APScheduler integration: periodic RSS feed fetching."""
import logging


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, func, or_, select

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
        # Resolve default interval from app_settings
        result = await session.execute(
            select(AppSettings.default_fetch_interval_min).where(AppSettings.id == 1)
        )
        default_interval = result.scalar_one_or_none() or 60

        # active: fetch when due; error: retry after 5× interval (min 30 min); paused: skip
        error_backoff_min = max(30, default_interval * 5)
        effective_interval = func.make_interval(mins=func.coalesce(Feed.fetch_interval_min, default_interval))
        backoff_interval = func.make_interval(mins=error_backoff_min)
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
    return scheduler

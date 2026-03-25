"""APScheduler integration: periodic RSS feed fetching."""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text

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

        # Feeds that are active and past their next scheduled fetch time
        due_feeds = await session.execute(
            select(Feed).where(
                Feed.status == "active",
                text(
                    "last_fetched_at IS NULL OR "
                    "last_fetched_at + (COALESCE(fetch_interval_min, :di) * interval '1 minute') < NOW()"
                ).bindparams(di=default_interval),
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

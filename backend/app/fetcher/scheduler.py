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


def _slot_matches(effective_interval_min: int, minute: int) -> bool:
    """Return True if a feed with *effective_interval_min* should be evaluated at *minute*.

    The scheduler fires at :00/:15/:30/:45. Each slot checks only feeds whose
    sub-hour period (interval % 60) aligns with that minute:
      :00  — all feeds
      :15/:45 — only 15-min feeds (sub_period == 15)
      :30  — 15-min and 30-min feeds, including 90-min (sub_period in {15, 30})

    Supported intervals are aligned to 15/30/60 (the UI offers
    [15,30,60,90,120,180,360,720,1440]). A value with sub_period not in {15,30}
    — e.g. 45 — would only be evaluated at :00; this is acceptable because such
    intervals are not selectable in the UI.
    """
    if minute == 0:
        return True
    sub_period = effective_interval_min % 60
    if minute in (15, 45):
        return sub_period == 15
    # minute == 30
    return sub_period in (15, 30)


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
        effective_interval_min = func.greatest(
            func.coalesce(Feed.fetch_interval_min, default_interval),
            min_interval,
        )
        effective_interval = effective_interval_min * one_minute

        # Slot pre-filter — see _slot_matches() for the full rule.
        # TODO: make this behaviour configurable (app_settings flag) if needed.
        minute = now.minute
        if minute == 0:
            slot_conditions = []
        elif minute in (15, 45):
            slot_conditions = [effective_interval_min % 60 == 15]
        else:  # minute == 30
            slot_conditions = [(effective_interval_min % 60).in_([15, 30])]
        # count 0–(threshold-1): regular backoff; count threshold+: 24 h, then disabled
        error_backoff = case(
            (Feed.fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD, literal_column("interval '24 hours'")),
            else_=literal_column(f"interval '{error_backoff_min} minutes'"),
        )
        grace = literal_column("interval '2 minutes'")
        due_feeds = await session.execute(
            select(Feed).where(
                Feed.subscriber_count > 0,
                *slot_conditions,
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
                        await fetch_scrape_feed(
                            feed_in_session, session,
                            published_cutoff=cutoff_by_feed.get(feed_id),
                        )
                    else:
                        await fetch_feed(
                            feed_in_session, session,
                            published_cutoff=cutoff_by_feed.get(feed_id),
                        )

    fetch_start = datetime.now(timezone.utc)
    results = await asyncio.gather(
        *[_fetch_one(feed.id) for feed in feeds], return_exceptions=True
    )
    for feed, result in zip(feeds, results):
        if isinstance(result, BaseException):
            logger.error(
                "Scheduler: unexpected error fetching feed %d", feed.id, exc_info=result
            )

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


async def _process_ai_summaries() -> None:
    """Job: process pending AI summary jobs."""
    if db.async_session_factory is None:
        return
    from app.services.ai_summary_service import process_pending_summaries
    async with db.async_session_factory() as session:
        await process_pending_summaries(session)


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


async def _cleanup_unverified_users() -> None:
    """Job: delete unverified user accounts older than 7 days."""
    if db.async_session_factory is None:
        return
    from sqlalchemy import delete
    from app.models.user import User
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    async with db.async_session_factory() as session:
        result = await session.execute(
            delete(User).where(
                User.email_verified.is_(False),
                User.created_at < cutoff,
            )
        )
        if result.rowcount:
            logger.info("Deleted %d unverified user accounts older than 7 days", result.rowcount)
        await session.commit()


async def _cleanup_expired_pending_emails() -> None:
    """Job: clear expired pending email-change requests."""
    if db.async_session_factory is None:
        return
    from sqlalchemy import update
    from app.models.user import User
    now = datetime.now(timezone.utc)
    async with db.async_session_factory() as session:
        result = await session.execute(
            update(User)
            .where(User.pending_email_expires_at < now)
            .values(
                pending_email=None,
                pending_email_token_hash=None,
                pending_email_expires_at=None,
            )
        )
        if result.rowcount:
            logger.info("Cleared %d expired pending email changes", result.rowcount)
        await session.commit()


async def _send_due_briefings() -> None:
    """Job: send scheduled briefing emails for all due configs."""
    import smtplib
    if db.async_session_factory is None:
        return

    from app.models.user import UserCatchupConfig, User, UserSettings
    from app.services.briefing_service import apply_briefing_failure, send_briefing
    from app.utils.smtp import send_email

    # Load app settings and due config IDs in a short-lived session
    async with db.async_session_factory() as session:
        app_settings_row = (await session.execute(
            select(AppSettings).where(AppSettings.id == 1)
        )).scalar_one_or_none()

        if not app_settings_row or not app_settings_row.ai_enabled:
            return

        now = datetime.now(timezone.utc)
        due_ids = (await session.execute(
            select(UserCatchupConfig.id).where(
                UserCatchupConfig.briefing_enabled.is_(True),
                UserCatchupConfig.briefing_next_send_at <= now,
            )
        )).scalars().all()

    # Process each config in its own isolated session
    for config_id in due_ids:
        async with db.async_session_factory() as session:
            config = (await session.execute(
                select(UserCatchupConfig).where(UserCatchupConfig.id == config_id)
            )).scalar_one_or_none()
            if not config or not config.briefing_enabled:
                continue

            user = (await session.execute(
                select(User).where(User.id == config.user_id)
            )).scalar_one_or_none()
            if not user:
                continue

            user_settings = (await session.execute(
                select(UserSettings).where(UserSettings.user_id == user.id)
            )).scalar_one_or_none()
            user.settings = user_settings
            tz_str = (user_settings.timezone if user_settings else None) or "UTC"

            try:
                await send_briefing(config, user, session, app_settings_row)
            except smtplib.SMTPException as exc:
                logger.error("Briefing SMTP error for config %d: %s", config_id, exc)
                apply_briefing_failure(config, exc, is_smtp=True, tz_str=tz_str)
                await session.commit()
            except Exception as exc:
                logger.error("Briefing error for config %d: %s", config_id, exc)
                notify = apply_briefing_failure(config, exc, is_smtp=False, tz_str=tz_str)
                await session.commit()
                if notify:
                    try:
                        send_email(
                            app_settings_row,
                            user.email,
                            f"Briefing failed: {config.name}",
                            f"Your briefing '{config.name}' could not be sent after 2 attempts.\n\nError: {exc}\n\nYou can check and re-enable it in Catch me up & Briefings.",
                        )
                    except Exception:
                        pass


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
        minutes=10,
        id="process_ai_filters",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _process_ai_summaries,
        trigger="interval",
        minutes=5,
        id="process_ai_summaries",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
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
    scheduler.add_job(
        _send_due_briefings,
        trigger="interval",
        minutes=15,
        id="send_due_briefings",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
    )
    scheduler.add_job(
        _cleanup_unverified_users,
        trigger="cron",
        hour=4,
        minute=0,
        id="cleanup_unverified_users",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _cleanup_expired_pending_emails,
        trigger="cron",
        hour=4,
        minute=10,
        id="cleanup_expired_pending_emails",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler

"""APScheduler integration: periodic RSS feed fetching and readable extraction."""
import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import and_, case, func, literal_column, or_, select

import app.database as db
from app.config import settings
from app.fetcher import host_throttle
from app.fetcher.host_throttle import host_key
from app.fetcher.interval import (
    WINDOW_DAYS,
    auto_interval_min,
    derive_interval_min,
)
from app.fetcher.rss import FETCH_ERROR_DISABLE_THRESHOLD, dedup_cross_feed_global, fetch_feed
from app.models.feed import Feed, UserFeed
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")

# Concurrency limits for a single scheduler fetch round.
_GLOBAL_FETCH_CONCURRENCY = 10
# Max simultaneous requests to one host. 1 fully serializes same-host feeds so a
# site (e.g. several Reddit feeds) is polled politely from our single IP instead
# of in a burst that trips HTTP 429. Tune here if a more parallel host appears.
_PER_HOST_CONCURRENCY = 1
# Budget for waiting out per-host cooldowns *within* a fetch round. The round is
# triggered every 15 min with max_instances=1, so it MUST finish before the next
# slot or that slot is missed entirely — we stop starting new in-round waits after
# this point (leaving ~3 min reserve) and defer the rest to the next round.
_ROUND_BUDGET = timedelta(minutes=12)
# Never tie a worker up on a single feed longer than this; a host asking for a
# longer reset is deferred to the next round instead.
_MAX_SINGLE_WAIT = timedelta(minutes=2)
# Slack added to a cooldown wait so we fetch safely *after* the window resets.
# Rate-limit `*-reset` values point at the window end but tend to undershoot it by
# a second or two (e.g. Reddit's per-minute window: reset counts down to the next
# :00 but lands ~1-3 s early), so a 1 s buffer still fetches inside the old window
# and 429s. 5 s clears the boundary reliably; negligible at a ~60 s cadence.
_COOLDOWN_BUFFER = timedelta(seconds=5)
# A feed counts as due up to this long before its nominal next-fetch time, so a feed
# whose timer elapses a minute or two after a tick is still picked at that tick
# instead of slipping a whole 15-min slot. Keep the selection query, its pure mirror
# (_feed_due_for_selection), and the UI prediction (compute_next_fetch_at) in sync.
_DUE_GRACE = timedelta(minutes=2)

# One quick re-check after a feed's first failure, before it settles into the
# regular error backoff. Most failures we see are a host having a moment (a 5xx, a
# timeout, a 404 served by a backend that is briefly confused about its own
# content), and those are over in minutes — while the regular backoff is two hours
# at the default interval, which is that long a hole in a healthy feed. A failure
# that is not transient costs exactly one extra request for this: from the second
# failure on, the feed is back on the regular backoff.
_FIRST_ERROR_RETRY_MIN = 30

# Phase offset (0–14 min) for the four 15-min fetch ticks. 0 keeps the historical
# :00/:15/:30/:45; a non-zero value (e.g. staging) shifts them so co-hosted instances
# don't fetch at the same wall-clock moment. Config already folds it into 0–14.
_SLOT_OFFSET_MIN = settings.fetch_schedule_offset_min % 15

_T = TypeVar("_T")


def effective_interval_min(
    feed: Feed,
    *,
    default_interval_min: int,
    min_interval_min: int,
    max_interval_min: int,
) -> int:
    """The feed's effective poll interval in minutes. Mirror of ``effective_interval_sql``.

    A manual override (``fetch_interval_min``) is authoritative: floored at the global
    minimum but NOT capped, since the user picked it explicitly. Otherwise (Auto) defer
    to :func:`app.fetcher.interval.auto_interval_min`, which caps a genuinely derived
    value but leaves the default fallback uncapped.
    """
    if feed.fetch_interval_min is not None:
        return max(feed.fetch_interval_min, min_interval_min)
    return auto_interval_min(
        feed.derived_interval_min,
        default_interval_min=default_interval_min,
        min_interval_min=min_interval_min,
        max_interval_min=max_interval_min,
    )


def effective_interval_sql(
    default_interval_min: int, min_interval_min: int, max_interval_min: int
):
    """SQLAlchemy expression mirroring :func:`effective_interval_min` for the due query.

    A DB test guards that this and the Python scalar agree across the manual/auto matrix.
    Like the scalar, only a genuinely derived value is capped; the default fallback
    (no derived value yet) is floored at the minimum but left uncapped.
    """
    manual = Feed.fetch_interval_min
    derived = Feed.derived_interval_min
    auto = case(
        (
            derived.isnot(None),
            func.least(func.greatest(derived, min_interval_min), max_interval_min),
        ),
        else_=func.greatest(default_interval_min, min_interval_min),
    )
    return case(
        (manual.isnot(None), func.greatest(manual, min_interval_min)),
        else_=auto,
    )


def _cooldown_wait(
    until: datetime | None, now: datetime, round_deadline: datetime
) -> timedelta | None:
    """Decide how to handle a host cooldown for a feed about to be fetched.

    Returns:
      * ``timedelta(0)`` — no active cooldown, fetch immediately.
      * a positive ``timedelta`` — sleep this long (reset + buffer) then fetch.
      * ``None`` — defer to the next round (the wait would blow the single-wait cap
        or push past the round budget).
    """
    if until is None or until <= now:
        return timedelta(0)
    wait = until - now
    if until > round_deadline or wait > _MAX_SINGLE_WAIT:
        return None
    return wait + _COOLDOWN_BUFFER


async def _run_throttled(
    items: Sequence[_T],
    worker: Callable[[_T], Awaitable[None]],
    *,
    global_limit: int,
    per_host_limit: int,
    host_of: Callable[[_T], str],
    on_host_ready: Callable[[_T], Awaitable[bool]] | None = None,
) -> list:
    """Run ``worker(item)`` for every item, bounded by a global concurrency limit
    and a per-host limit.

    The per-host gate is acquired *outside* the global one, so an item waiting on a
    busy host does not hold a global slot (no starvation of the global pool). The
    per-host semaphores live only for this call — throttling is scoped to one fetch
    round, which is exactly the burst we want to flatten. Mirrors
    ``asyncio.gather(..., return_exceptions=True)``.

    ``on_host_ready`` (optional) runs under the per-host gate but *before* the global
    slot is taken; returning False skips the item (worker not run). Any waiting it
    does therefore holds only the host gate, not a global slot — so a cooling-down
    host can't tie up global capacity that healthy hosts could use.
    """
    global_sem = asyncio.Semaphore(global_limit)
    host_sems: dict[str, asyncio.Semaphore] = {}

    def _host_sem(host: str) -> asyncio.Semaphore:
        sem = host_sems.get(host)
        if sem is None:
            sem = asyncio.Semaphore(per_host_limit)
            host_sems[host] = sem
        return sem

    async def _run(item: _T) -> None:
        async with _host_sem(host_of(item)):
            if on_host_ready is not None and not await on_host_ready(item):
                return
            async with global_sem:
                await worker(item)

    return await asyncio.gather(*[_run(item) for item in items], return_exceptions=True)


def _ceil_to_slot(dt: datetime, offset: int = _SLOT_OFFSET_MIN) -> datetime:
    """Round *dt* up to the next scheduler tick.

    Ticks fall every 15 min at minutes congruent to *offset* (mod 15); *offset* 0
    gives the historical :00/:15/:30/:45. Mirrors the cron trigger built in
    :func:`create_scheduler` so the UI next-fetch prediction lands on the real tick.
    """
    floored = dt.replace(second=0, microsecond=0)
    rem = (floored.minute - offset) % 15
    if rem == 0 and floored == dt:
        return floored
    return floored - timedelta(minutes=rem) + timedelta(minutes=15)


def error_backoff_minutes(fetch_error_count: int, default_interval_min: int) -> int:
    """How long a feed in the ``error`` status waits before the next attempt.

    Three tiers, by consecutive-failure count: one quick re-check after a single
    failure (see ``_FIRST_ERROR_RETRY_MIN``), the regular backoff while it keeps
    failing, and 24 h once it is past the disable threshold — at which point the
    next failure retires it anyway, so this only paces a feed that got there and
    then started succeeding intermittently.

    The quick re-check is floored by the regular backoff: a deployment with a short
    global interval can compute a regular backoff under 30 min, and a first retry
    slower than the later ones would be plainly wrong.

    This is the Python side of the ``error_backoff`` CASE in :func:`_select_due_feeds`;
    a DB test guards the two against drift.
    """
    regular = max(15, default_interval_min * 2)
    if fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD:
        return 24 * 60
    if fetch_error_count <= 1:
        return min(_FIRST_ERROR_RETRY_MIN, regular)
    return regular


def compute_next_fetch_at(
    feed: Feed,
    *,
    default_interval_min: int,
    min_interval_min: int,
    max_interval_min: int,
    now: datetime | None = None,
) -> datetime | None:
    """Predict when the scheduler will next attempt to fetch *feed*.

    Mirrors the due-feed query in :func:`_fetch_due_feeds` so the UI can show a
    feed's next fetch without persisting it. Returns a timezone-aware datetime,
    or ``None`` when no fetch is scheduled — paused/disabled feeds and feeds with
    no subscribers are never queried by the scheduler.
    """
    now = now or datetime.now(timezone.utc)
    if feed.subscriber_count <= 0 or feed.status not in ("active", "error"):
        return None

    interval_min = effective_interval_min(
        feed,
        default_interval_min=default_interval_min,
        min_interval_min=min_interval_min,
        max_interval_min=max_interval_min,
    )

    if feed.last_fetched_at is None:
        due = now
    elif feed.status == "error":
        backoff_min = error_backoff_minutes(feed.fetch_error_count, default_interval_min)
        due = feed.last_fetched_at + timedelta(minutes=backoff_min)
    else:  # active
        due = feed.last_fetched_at + timedelta(minutes=interval_min)

    # A server-requested Retry-After (HTTP 429) defers the feed further.
    if feed.retry_after_until is not None and feed.retry_after_until > due:
        due = feed.retry_after_until

    # The scheduler fires every 15 min at :00/:15/:30/:45 and counts a feed due up
    # to _DUE_GRACE early. The feed fetches at the first such tick at or after its
    # due time — any tick, no longer forced onto the top of the hour.
    target = max(due - _DUE_GRACE, now)
    return _ceil_to_slot(target)


def _feed_due_for_selection(
    *,
    effective_interval_min: int,
    status: str,
    last_fetched_at: datetime | None,
    retry_after_until: datetime | None,
    error_backoff_min: int,
    now: datetime,
    grace: timedelta = _DUE_GRACE,
) -> bool:
    """Pure mirror of the :func:`_select_due_feeds` WHERE clause: would this feed be
    picked at a scheduler tick at *now*?

    A feed is due when it is not deferred by a server ``Retry-After`` and its
    per-status timer has elapsed (interval for ``active``, tiered backoff for
    ``error``, both with a small *grace*). There is no wall-clock slot alignment: a
    due feed is picked at whichever of the four ticks first follows its timer, so
    feeds spread across the hour on their own natural phase instead of piling up at
    :00.

    This documents the rule and is unit-tested; the real query in
    :func:`_select_due_feeds` is the source of truth and a DB test guards drift.
    """
    if retry_after_until is not None and retry_after_until >= now + grace:
        return False
    interval = timedelta(minutes=effective_interval_min)
    backoff = timedelta(minutes=error_backoff_min)
    if status == "active":
        return last_fetched_at is None or last_fetched_at + interval < now + grace
    if status == "error":
        return last_fetched_at is not None and last_fetched_at + backoff < now + grace
    return False


async def _select_due_feeds(
    session, now: datetime, *, default_interval: int, min_interval: int, max_interval: int
) -> list[Feed]:
    """Select the feeds due for a fetch at *now* (the scheduler's selection query).

    Factored out of :func:`_fetch_due_feeds` and parameterised on *now* (rather than
    the DB clock) so the due rule can be tested deterministically. See
    :func:`_feed_due_for_selection` for the rule in prose.
    """
    regular_backoff_min = error_backoff_minutes(2, default_interval)
    first_retry_min = error_backoff_minutes(1, default_interval)
    one_minute = literal_column("interval '1 minute'")
    # manual override (uncapped) or adaptive derived interval, clamped — see
    # effective_interval_sql(); a DB test guards it against the Python mirror.
    effective_interval = (
        effective_interval_sql(default_interval, min_interval, max_interval) * one_minute
    )
    # SQL mirror of error_backoff_minutes(): count 0–1 a quick re-check, then the
    # regular backoff, and 24 h from the disable threshold on.
    error_backoff = case(
        (Feed.fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD, literal_column("interval '24 hours'")),
        (Feed.fetch_error_count <= 1, literal_column(f"interval '{first_retry_min} minutes'")),
        else_=literal_column(f"interval '{regular_backoff_min} minutes'"),
    )
    due_cutoff = now + _DUE_GRACE

    # A feed is due whenever its per-status timer has elapsed (with grace); it is then
    # fetched at whichever 15-min tick this is. No wall-clock slot alignment — feeds
    # ride their own phase and spread across the hour. See _feed_due_for_selection().
    result = await session.execute(
        select(Feed).where(
            Feed.subscriber_count > 0,
            # Honor a server Retry-After (HTTP 429): skip until it passes.
            or_(
                Feed.retry_after_until.is_(None),
                Feed.retry_after_until < due_cutoff,
            ),
            or_(
                and_(
                    Feed.status == "active",
                    or_(
                        Feed.last_fetched_at.is_(None),
                        Feed.last_fetched_at + effective_interval < due_cutoff,
                    ),
                ),
                and_(
                    Feed.status == "error",
                    Feed.last_fetched_at + error_backoff < due_cutoff,
                ),
            ),
        )
    )
    return list(result.scalars().all())


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
                AppSettings.max_fetch_interval_min,
                AppSettings.default_purge_after_days,
            ).where(AppSettings.id == 1)
        )
        row = result.one_or_none()
        default_interval = (row[0] if row else None) or 60
        min_interval = (row[1] if row else None) or 15
        max_interval = (row[2] if row else None) or 360
        global_purge_days = (row[3] if row else None)
        now = datetime.now(timezone.utc)

        # active: fetch when due; error: tiered backoff; paused/disabled: skip.
        # A due feed is picked at any 15-min tick — see _select_due_feeds().
        feeds = await _select_due_feeds(
            session, now, default_interval=default_interval,
            min_interval=min_interval, max_interval=max_interval,
        )

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

    async def _await_host_ready(feed: Feed) -> bool:
        """Per-host cooldown gate (bounded hybrid), run under the host semaphore but
        before a global slot is taken. Wait the reset out in-round as long as it fits
        the round budget and the single-wait cap; otherwise defer the feed to the next
        round (return False). Waiting (rather than deferring) lets a rate-limited host
        like Reddit drain several feeds per 15-min round instead of just one, while the
        budget keeps the round short enough not to miss the next slot. Sleeping here
        holds only the host gate, so it never ties up a global slot healthy hosts want.
        """
        now = datetime.now(timezone.utc)
        host = host_key(feed.feed_url)
        # include_block: the scheduler also backs off on the 403 anti-bot breather
        # (manual refreshes don't — see host_throttle module docstring).
        until = host_throttle.blocked_until(host, now, include_block=True)
        delay = _cooldown_wait(until, now, round_deadline)
        if delay is None:
            logger.info(
                "Scheduler: deferring feed %d — host %s cooling down %.0fs",
                feed.id, host, (until - now).total_seconds(),
            )
            return False
        if delay:
            await asyncio.sleep(delay.total_seconds())
        return True

    async def _fetch_one(feed_id: int) -> None:
        if feed_id in _initial_fetch_in_progress:
            logger.debug("Scheduler: skipping feed %d — initial fetch in progress", feed_id)
            return
        async with db.async_session_factory() as session:
            feed_in_session = await session.get(Feed, feed_id)
            if feed_in_session and feed_in_session.id not in _initial_fetch_in_progress:
                # Capture the URL now: fetch_feed commits, which expires ORM
                # attributes, and re-reading feed_url afterwards would trigger an
                # implicit sync reload the async session forbids (MissingGreenlet).
                feed_url = feed_in_session.feed_url
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
                # Pace the host after this fetch: a scheduler breather at the learned
                # gap, plus a manual-visible cooldown when a real limit was learned (so
                # the next same-host feed this round — and manual refreshes — hold off
                # instead of hammering into a known rate-limit gap). The fetch above just
                # refreshed the learned value for this host.
                host_throttle.arm_after_fetch(
                    host_key(feed_url), datetime.now(timezone.utc)
                )

    fetch_start = datetime.now(timezone.utc)
    round_deadline = fetch_start + _ROUND_BUDGET
    results = await _run_throttled(
        feeds,
        lambda f: _fetch_one(f.id),
        global_limit=_GLOBAL_FETCH_CONCURRENCY,
        per_host_limit=_PER_HOST_CONCURRENCY,
        host_of=lambda f: host_key(f.feed_url),
        on_host_ready=_await_host_ready,
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
        # Persist any learned per-host spacing changed this round (batched write-back).
        from app.services.host_rate_limit_service import flush
        await flush(session)


async def _process_readable() -> None:
    """Job: extract readable content for pending articles."""
    if db.async_session_factory is None:
        return
    from app.services.readable_service import process_pending_readable
    async with db.async_session_factory() as session:
        await process_pending_readable(session)


async def _retry_blocked_readable() -> None:
    """Job: probe feeds whose readable extraction was auto-disabled for 403s."""
    if db.async_session_factory is None:
        return
    from app.services.readable_service import retry_blocked_feeds
    async with db.async_session_factory() as session:
        await retry_blocked_feeds(session)


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


async def recompute_derived_intervals(session) -> int:
    """Recompute ``Feed.derived_interval_min`` for every feed from its recent publish
    cadence, writing only the feeds whose value changed. Returns the number updated.

    One grouped count over the trailing window drives the whole set; cadence moves
    slowly, so this runs daily (plus once at startup) rather than per fetch.

    The count filters ``trimmed_at IS NULL`` so it can ride the partial index
    ``ix_articles_sort_ts``. This can slightly undercount a very high-volume feed whose
    recent items were already trimmed by count-based purge, but such feeds bottom out at
    ``AUTO_FLOOR`` regardless, so the effect is immaterial — a deliberate trade-off.
    Only ``active``/``error`` feeds are recomputed; paused/disabled ones are not polled,
    so a stale derived value on them costs nothing (the next run refreshes on resume).
    """
    from app.models.article import Article

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=WINDOW_DAYS)
    rows = await session.execute(
        select(Article.feed_id, func.count().label("n"))
        .where(
            func.coalesce(Article.published_at, Article.fetched_at) > window_start,
            Article.trimmed_at.is_(None),
        )
        .group_by(Article.feed_id)
    )
    counts = {feed_id: n for feed_id, n in rows.all()}

    feeds = (
        await session.execute(
            select(Feed).where(Feed.status.in_(("active", "error")))
        )
    ).scalars().all()
    updated = 0
    for feed in feeds:
        new_val = derive_interval_min(
            created_at=feed.created_at, count=counts.get(feed.id, 0), now=now
        )
        if new_val != feed.derived_interval_min:
            feed.derived_interval_min = new_val
            updated += 1
    if updated:
        await session.commit()
    return updated


async def _recompute_derived_intervals() -> None:
    """Job: refresh the adaptive per-feed fetch intervals."""
    if db.async_session_factory is None:
        return
    async with db.async_session_factory() as session:
        n = await recompute_derived_intervals(session)
        if n:
            logger.info("Recomputed adaptive fetch interval for %d feeds", n)


async def _purge_old_articles() -> None:
    """Job: delete articles exceeding retention limits, and stale fetch logs with them."""
    if db.async_session_factory is None:
        return
    from app.services.purge_service import purge_old_articles, purge_old_fetch_logs
    async with db.async_session_factory() as session:
        await purge_old_articles(session)
        # Own session-level commit, so a failure here cannot undo the article pass.
        await purge_old_fetch_logs(session)


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


async def _sweep_thumb_cache() -> None:
    """Job: drop video thumbnails nobody has requested within the idle window."""
    from app.services.video_thumb_service import sweep_idle_thumbnails
    await asyncio.to_thread(sweep_idle_thumbnails)


async def _generate_due_preferences() -> None:
    """Job: regenerate interest profiles for users who have it on a schedule.

    Candidates are only checked here; ``run_auto_generation`` owns the decision
    whether a run is actually due. Generations are capped per run because a
    quality-model call takes seconds and the batch is sequential — skips are
    plain SQL and do not count against the cap.
    """
    if db.async_session_factory is None:
        return

    from app.models.user import User, UserSettings
    from app.services.ai_profile_service import run_auto_generation

    max_generations = 50

    async with db.async_session_factory() as session:
        app_settings_row = (await session.execute(
            select(AppSettings).where(AppSettings.id == 1)
        )).scalar_one_or_none()
        if not app_settings_row or not app_settings_row.ai_enabled:
            return

        # Users inactive for a month drop out entirely; they re-enter on their
        # next visit (last_active_at is bumped hourly while browsing).
        active_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        user_ids = (await session.execute(
            select(UserSettings.user_id)
            .join(User, User.id == UserSettings.user_id)
            .where(
                UserSettings.ai_preference_auto_days > 0,
                UserSettings.ai_scoring_enabled_default.is_(True),
                User.is_active.is_(True),
                User.last_active_at >= active_cutoff,
            )
            .order_by(UserSettings.ai_preference_updated_at.asc().nulls_first())
        )).scalars().all()

    generated = skipped = failed = 0
    for user_id in user_ids:
        if generated >= max_generations:
            break
        async with db.async_session_factory() as session:
            try:
                outcome = await run_auto_generation(user_id, session)
            except Exception as exc:  # never let one user stop the batch
                logger.error("Auto profile job crashed for user %d: %s", user_id, exc)
                failed += 1
                continue
        if outcome == "generated":
            generated += 1
        elif outcome.startswith("failed"):
            failed += 1
        else:
            skipped += 1

    if generated or failed:
        logger.info(
            "Auto interest profiles: generated=%d skipped=%d failed=%d",
            generated, skipped, failed,
        )


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
                            # briefing_last_error, not exc: apply_briefing_failure
                            # has just resolved what actually went wrong, and the
                            # raw exception can be the SDK's empty "Connection
                            # error." where that says a refused address.
                            f"Your briefing '{config.name}' could not be sent after 2 attempts.\n\nError: {config.briefing_last_error}\n\nYou can check and re-enable it in Catch me up & Briefings.",
                        )
                    except Exception:
                        pass


def create_scheduler() -> AsyncIOScheduler:
    """Configure and return the scheduler (not yet started)."""
    fetch_minutes = ",".join(str(_SLOT_OFFSET_MIN + 15 * k) for k in range(4))
    scheduler.add_job(
        _fetch_due_feeds,
        trigger="cron",
        minute=fetch_minutes,
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
        _recompute_derived_intervals,
        trigger="cron",
        hour=2,
        minute=30,
        id="recompute_derived_intervals",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
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
        _generate_due_preferences,
        trigger="cron",
        hour=4,
        minute=20,
        id="generate_due_preferences",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _retry_blocked_readable,
        trigger="cron",
        hour=4,
        minute=40,
        id="retry_blocked_readable",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=3600,
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
        _sweep_thumb_cache,
        trigger="cron",
        hour=4,
        minute=50,
        id="sweep_thumb_cache",
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

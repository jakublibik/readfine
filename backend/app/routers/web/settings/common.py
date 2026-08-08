"""Shared helpers used across the settings sub-routers.

Only helpers referenced by 2+ areas live here; single-area helpers stay in their
own module.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.fetcher.interval import auto_interval_min
from app.fetcher.scheduler import compute_next_fetch_at
from app.models.article import Article
from app.models.feed import Feed, Folder, UserFeed
from app.models.settings import AppSettings
from app.models.user import User, UserSettings
from app.services.feed import list_user_feeds
from app.utils.datetime_format import format_until
from app.utils.parsing import safe_int


def _ensure_scheme(url: str) -> str:
    """Prefix https:// when a user-entered URL omits the scheme."""
    return f"https://{url}" if url and "://" not in url else url


async def _scrape_target_url(form, user: User, db: AsyncSession) -> str:
    """The page the scrape helpers (preview, AI selector, prompt) should fetch.

    An existing feed is addressed by ``feed_id`` and its URL is read from the
    database, so the edit forms never have to carry the stored address in a hidden
    field: it can hold an API token or HTTP credentials. The setup flow has no feed
    row yet and still passes the URL the user just typed.

    Returns "" when the id is unknown or the user has no claim to that feed, which
    the callers report the same way as a missing URL.
    """
    feed_id = safe_int(form.get("feed_id"))
    if feed_id is not None:
        stmt = select(Feed.feed_url).where(Feed.id == feed_id)
        if user.role != "admin":
            stmt = stmt.join(UserFeed, UserFeed.feed_id == Feed.id).where(
                UserFeed.user_id == user.id
            )
        return await db.scalar(stmt) or ""
    return _ensure_scheme((form.get("url") or "").strip())


def _snap_interval(raw: int) -> int:
    """Clamp a fetch interval to [15, 1440] minutes, snapped to the nearest 15."""
    return max(15, min(1440, round(raw / 15) * 15))


def _ai_selector_available(app_s, user_s) -> bool:
    """True when AI CSS-selector generation is usable: AI enabled globally and the
    user has a quality model configured."""
    return bool(
        app_s and app_s.ai_enabled
        and user_s and user_s.ai_quality_provider and user_s.ai_quality_model
    )


async def _get_or_create_settings(user: User, db: AsyncSession) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    s = result.scalar_one_or_none()
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
        await db.flush()
    return s


async def _get_feeds_context(user, db):
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    feed_ids = [uf.feed_id for uf in user_feeds]
    if feed_ids:
        counts_result = await db.execute(
            select(Article.feed_id, func.count(Article.id).label("cnt"))
            .where(Article.feed_id.in_(feed_ids))
            .group_by(Article.feed_id)
        )
        article_counts = {row.feed_id: row.cnt for row in counts_result}
    else:
        article_counts = {}
    # Annotate each feed (transient, in-memory) with its effective Auto interval and
    # predicted next fetch (relative hint) for the feeds table.
    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    default_interval = (app_s.default_fetch_interval_min if app_s else None) or 60
    min_interval = (app_s.min_fetch_interval_min if app_s else None) or 15
    max_interval = (app_s.max_fetch_interval_min if app_s else None) or 360
    now = datetime.now(timezone.utc)
    for uf in user_feeds:
        f = uf.feed
        f.auto_interval_min = auto_interval_min(
            f.derived_interval_min, default_interval_min=default_interval,
            min_interval_min=min_interval, max_interval_min=max_interval,
        )
        f.next_fetch_at = compute_next_fetch_at(
            f, default_interval_min=default_interval,
            min_interval_min=min_interval, max_interval_min=max_interval, now=now,
        )
        f.next_fetch_rel = format_until(f.next_fetch_at, now)
    return user_feeds, folders, article_counts

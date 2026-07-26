"""Admin service: user management, app settings, invitations, audit log."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import (
    AiUsageLog,
    Article,
    ArticleAiChat,
    ArticleAiJob,
    GeneralChatLog,
    UserArticleState,
)
from app.models.auth import Invitation
from app.models.feed import Feed, UserFeed
from app.models.filter import Filter
from app.models.fetch_log import FetchLog
from app.models.settings import AppSettings, AuditLog
from app.models.user import User, UserCatchupConfig, UserSettings
from app.auth.security import generate_token
from app.services.scope_cleanup import strip_scope_references

logger = logging.getLogger(__name__)


async def get_app_settings(db: AsyncSession) -> AppSettings:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    s = result.scalar_one_or_none()
    if s is None:
        s = AppSettings(id=1)
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return s


async def update_app_settings(db: AsyncSession, data: dict) -> AppSettings:
    s = await get_app_settings(db)
    for key, value in data.items():
        if hasattr(s, key):
            setattr(s, key, value)
    s.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(s)
    from app.services.app_settings_cache import invalidate_registration_cache
    invalidate_registration_cache()
    return s


async def list_users(db: AsyncSession) -> list[dict]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=7)

    async def _counts(stmt) -> dict[int, int]:
        return {row[0]: row[1] for row in (await db.execute(stmt)).all()}

    feed_counts = await _counts(
        select(UserFeed.user_id, func.count(UserFeed.feed_id)).group_by(UserFeed.user_id)
    )
    article_counts = await _counts(
        select(UserFeed.user_id, func.count(Article.id))
        .join(Article, Article.feed_id == UserFeed.feed_id)
        .group_by(UserFeed.user_id)
    )
    filter_counts = await _counts(
        select(Filter.user_id, func.count(Filter.id)).group_by(Filter.user_id)
    )
    # Genuine reading in the last 7 days. Uses read_at (set on mark-read) gated by
    # dwell >= 30s — the same "reading happened" signal stats_service uses for streaks
    # and heatmaps. link_opened has no timestamp, so it can't be time-bounded here.
    read_counts = await _counts(
        select(UserArticleState.user_id, func.count())
        .where(
            UserArticleState.read_at >= cutoff,
            UserArticleState.dwell_seconds >= 30,
        )
        .group_by(UserArticleState.user_id)
    )

    # AI usage in the last 7 days, aggregated across all sources per user.
    # We only care whether a user actively uses AI, not lifetime totals.
    ai_recent: dict[int, int] = {}

    async def _ai_counts(model, ts_col, *conds) -> None:
        stmt = select(
            model.user_id,
            func.count().filter(ts_col >= cutoff),
        ).group_by(model.user_id)
        for cond in conds:
            stmt = stmt.where(cond)
        for uid, recent in (await db.execute(stmt)).all():
            ai_recent[uid] = ai_recent.get(uid, 0) + recent

    await _ai_counts(ArticleAiJob, ArticleAiJob.created_at, ArticleAiJob.status == "success")
    await _ai_counts(AiUsageLog, AiUsageLog.created_at)
    await _ai_counts(ArticleAiChat, ArticleAiChat.created_at)
    await _ai_counts(GeneralChatLog, GeneralChatLog.created_at)

    users = (
        await db.execute(select(User).order_by(User.created_at.desc()))
    ).scalars().all()
    result = []
    for user in users:
        if user.last_active_at:
            last_active = (
                user.last_active_at.replace(tzinfo=timezone.utc)
                if user.last_active_at.tzinfo is None
                else user.last_active_at
            )
            inactive_days = (now - last_active).days
        else:
            inactive_days = None
        result.append({
            "user": user,
            "feed_count": feed_counts.get(user.id, 0),
            "article_count": article_counts.get(user.id, 0),
            "filter_count": filter_counts.get(user.id, 0),
            "read_count": read_counts.get(user.id, 0),
            "ai_ops_recent": ai_recent.get(user.id, 0),
            "inactive_days": inactive_days,
        })
    return result


async def toggle_user_active(db: AsyncSession, user_id: int, admin_id: int) -> User | None:
    """Toggle is_active on a user. Cannot deactivate yourself."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.id == admin_id:
        return None
    user.is_active = not user.is_active
    await db.commit()
    await db.refresh(user)
    return user


async def delete_user(db: AsyncSession, user_id: int, admin_id: int) -> bool:
    """Delete a user and all their data. Cannot delete yourself."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.id == admin_id:
        return False
    from app.services.feed import cleanup_user_feeds
    await cleanup_user_feeds(user_id, db)
    await db.delete(user)
    await db.commit()
    return True


async def list_invitations(db: AsyncSession) -> list[Invitation]:
    result = await db.execute(
        select(Invitation)
        .options(selectinload(Invitation.creator), selectinload(Invitation.used_by_user))
        .order_by(Invitation.created_at.desc())
    )
    return result.scalars().all()


async def create_invitation(
    db: AsyncSession,
    admin_id: int,
    email: str | None,
    expires_at: datetime | None,
) -> Invitation:
    inv = Invitation(
        created_by=admin_id,
        token=generate_token(),
        email=email or None,
        expires_at=expires_at,
    )
    db.add(inv)
    await db.commit()
    await db.refresh(inv)
    return inv


async def revoke_invitation(db: AsyncSession, invitation_id: int) -> "Invitation | None":
    result = await db.execute(select(Invitation).where(Invitation.id == invitation_id))
    inv = result.scalar_one_or_none()
    if not inv or inv.used_at is not None:
        return None
    await db.delete(inv)
    await db.commit()
    return inv


async def list_fetch_logs(db: AsyncSession, limit: int = 100) -> list[FetchLog]:
    result = await db.execute(
        select(FetchLog)
        .options(selectinload(FetchLog.feed))
        .order_by(FetchLog.failed_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def list_briefing_errors(db: AsyncSession) -> list[UserCatchupConfig]:
    """Catch-up configs whose scheduled briefing currently has an unresolved error.

    ``briefing_last_error`` is cleared on the next successful send
    (``briefing_service``), so a non-null value means the briefing has not
    recovered — the list only ever surfaces currently-broken configs. Configs
    with ``briefing_next_send_at IS NULL`` have no auto-retry scheduled (e.g.
    SMTP not configured, scope error) and need manual attention, so they sort
    first.
    """
    result = await db.execute(
        select(UserCatchupConfig)
        .options(selectinload(UserCatchupConfig.user))
        .where(UserCatchupConfig.briefing_last_error.is_not(None))
        .order_by(UserCatchupConfig.briefing_next_send_at.asc().nulls_first())
    )
    return result.scalars().all()


async def list_auto_profile_errors(db: AsyncSession) -> list[UserSettings]:
    """Users whose scheduled interest-profile update currently has an unresolved error.

    ``ai_preference_last_error`` is cleared by the next successful generation
    (``ai_profile_service``), so a non-null value means it has not recovered.
    Most causes are the user's own (API key, credit), but on a hosted instance a
    cluster of them is worth seeing. Rows where the schedule already switched
    itself off sort first — those never retry on their own.
    """
    result = await db.execute(
        select(UserSettings)
        .options(selectinload(UserSettings.user))
        .where(UserSettings.ai_preference_last_error.is_not(None))
        .order_by(
            UserSettings.ai_preference_auto_days.asc(),
            UserSettings.ai_preference_last_error_at.desc().nulls_last(),
        )
    )
    return result.scalars().all()


async def list_redirect_conflicts(db: AsyncSession) -> list[dict]:
    """Feeds that permanently redirect onto a URL another feed already holds.

    The stored address cannot be rewritten in that case (it would collide on the
    public-feed unique index), so the feed keeps walking its redirect on every
    fetch. New convergence no longer arises, since subscribe and OPML import resolve
    the address before creating a row, so a non-empty list is an existing pair worth
    merging by hand. Reads the in-process registry populated by ``adopt_permanent_url``
    and joins in the feed titles; returns newest-detected first.
    """
    from app.fetcher.redirects import redirect_conflicts
    from app.utils.url_validator import redact_url

    conflicts = redirect_conflicts()
    if not conflicts:
        return []
    ids = set(conflicts) | {c.holder_id for c in conflicts.values()}
    titles = dict((await db.execute(
        select(Feed.id, Feed.title).where(Feed.id.in_(ids))
    )).all())
    rows = [
        {
            "feed_id": feed_id,
            "feed_title": titles.get(feed_id, "—"),
            # Redacted: the query is unchanged from the feed's own URL (the adoption
            # guard requires it), so it can carry a token like ?api_key=… that the
            # dashboard should not print in full.
            "target_url": redact_url(c.target_url),
            "holder_id": c.holder_id,
            "holder_title": titles.get(c.holder_id, "(deleted)"),
            "detected_at": c.detected_at,
        }
        for feed_id, c in conflicts.items()
    ]
    rows.sort(key=lambda r: r["detected_at"], reverse=True)
    return rows


async def list_audit_logs(db: AsyncSession, limit: int = 100) -> list[AuditLog]:
    result = await db.execute(
        select(AuditLog)
        .options(selectinload(AuditLog.admin))
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def log_audit(
    db: AsyncSession,
    admin_id: int,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    detail: dict | None = None,
) -> None:
    db.add(AuditLog(
        admin_id=admin_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        detail=detail,
    ))
    await db.commit()


async def get_feed(db: AsyncSession, feed_id: int) -> Feed | None:
    return await db.get(Feed, feed_id)


async def toggle_feed_pause(db: AsyncSession, feed_id: int) -> Feed | None:
    feed = await db.get(Feed, feed_id)
    if not feed or feed.status == "error":
        return None
    feed.status = "paused" if feed.status == "active" else "active"
    await db.commit()
    await db.refresh(feed)
    return feed


async def clear_feed_error(db: AsyncSession, feed_id: int) -> Feed | None:
    feed = await db.get(Feed, feed_id)
    if not feed or feed.status != "error":
        return None
    feed.status = "active"
    feed.last_error = None
    feed.fetch_error_count = 0
    await db.commit()
    await db.refresh(feed)
    return feed


# Statuses an admin may set manually from the feed-edit form. 'error' is
# excluded — it's set by the fetcher, not chosen; use 'disabled' to turn a feed
# off by hand.
_ADMIN_EDITABLE_STATUSES = ("active", "paused", "disabled")


async def update_feed_admin(
    db: AsyncSession,
    feed_id: int,
    *,
    title: str,
    fetch_interval_min: int | None,
    status: str,
    article_links_selector: str | None = None,
) -> Feed | None:
    """Update feed-wide fields from the admin panel. Only touches columns that
    belong to the shared ``Feed`` (never per-user ``UserFeed`` preferences)."""
    feed = await db.get(Feed, feed_id)
    if not feed:
        return None
    title = (title or "").strip()
    if title:
        feed.title = title[:255]
    feed.fetch_interval_min = fetch_interval_min
    if status in _ADMIN_EDITABLE_STATUSES:
        # Bringing a feed back to active from a broken/off state clears the
        # error trail so the scheduler resumes cleanly (mirrors clear_feed_error).
        if status == "active" and feed.status in ("error", "disabled"):
            feed.last_error = None
            feed.fetch_error_count = 0
        feed.status = status
    if feed.feed_type == "scrape" and article_links_selector is not None:
        sel = article_links_selector.strip()
        if sel:
            feed.type_config = {**(feed.type_config or {}), "article_links_selector": sel}
    await db.commit()
    await db.refresh(feed)
    return feed


async def delete_feed(db: AsyncSession, feed_id: int) -> bool:
    feed = await db.get(Feed, feed_id)
    if not feed or feed.subscriber_count > 0:
        return False
    # Strip any lingering references to this feed from every user's filter and
    # catchup/briefing scopes (self-service unsubscribe already cleans its own,
    # so this mainly clears legacy dangling refs). No user is present to report to.
    await strip_scope_references(db, kind="feed", ref_id=feed_id, user_id=None)
    await db.execute(delete(Article).where(Article.feed_id == feed_id))
    await db.delete(feed)
    await db.commit()
    return True


# Admin feed sort priority: surface feeds that need attention first — broken
# (error) → auto-killed (disabled) → intentionally off (paused) → active. Applied
# both to the flat A–Z list and within each host group so the two views agree.
_STATUS_PRIORITY = {"error": 0, "disabled": 1, "paused": 2, "active": 3}


async def list_feeds_with_stats(db: AsyncSession) -> list[dict]:
    article_counts = (
        select(Article.feed_id, func.count(Article.id).label("article_count"))
        .group_by(Article.feed_id)
        .subquery()
    )
    status_priority = case(_STATUS_PRIORITY, value=Feed.status, else_=len(_STATUS_PRIORITY))
    rows = (await db.execute(
        select(Feed, func.coalesce(article_counts.c.article_count, 0))
        .outerjoin(article_counts, article_counts.c.feed_id == Feed.id)
        .order_by(status_priority, func.lower(Feed.title))
    )).all()
    return [{"feed": row[0], "article_count": row[1]} for row in rows]


def _feed_sort_key(item: dict) -> tuple:
    """Within a group: status priority (see _STATUS_PRIORITY), then title A–Z."""
    f = item["feed"]
    return (_STATUS_PRIORITY.get(f.status, len(_STATUS_PRIORITY)), (f.title or "").lower())


def group_feeds_by_host(items: list[dict]) -> list[dict]:
    """Group admin feed rows by their fetch host (same key as the rate-limit view).

    Hosts with ≥2 feeds become a named group; single-feed hosts collapse into an
    "Other" bucket, split into an errors bucket and a clean bucket. Ordering:
    named-with-error (A–Z host) → Other·errors → named-clean (A–Z host) → Other.
    """
    from app.fetcher.host_throttle import host_key  # noqa: PLC0415

    by_host: dict[str, list[dict]] = {}
    for it in items:
        by_host.setdefault(host_key(it["feed"].feed_url) or "", []).append(it)

    named: list[dict] = []
    other_error: list[dict] = []
    other_clean: list[dict] = []
    for host, feeds in by_host.items():
        if len(feeds) >= 2:
            feeds = sorted(feeds, key=_feed_sort_key)
            named.append({
                "kind": "named",
                "host": host,
                "label": host or "(unknown host)",
                "count": len(feeds),
                "has_error": any(x["feed"].status == "error" for x in feeds),
                "feeds": feeds,
            })
        elif feeds[0]["feed"].status == "error":
            other_error.append(feeds[0])
        else:
            other_clean.append(feeds[0])

    named.sort(key=lambda g: g["host"])
    ordered: list[dict] = [g for g in named if g["has_error"]]
    if other_error:
        ordered.append({
            "kind": "other_errors", "host": "", "label": "Other · errors",
            "count": len(other_error), "has_error": True,
            "feeds": sorted(other_error, key=_feed_sort_key),
        })
    ordered += [g for g in named if not g["has_error"]]
    if other_clean:
        ordered.append({
            "kind": "other", "host": "", "label": "Other",
            "count": len(other_clean), "has_error": False,
            "feeds": sorted(other_clean, key=_feed_sort_key),
        })
    return ordered


async def get_dashboard_stats(db: AsyncSession) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    user_count = (await db.execute(select(func.count(User.id)))).scalar() or 0
    active_user_count = (await db.execute(
        select(func.count(User.id)).where(User.is_active == True)
    )).scalar() or 0
    feed_count = (await db.execute(select(func.count(Feed.id)))).scalar() or 0
    article_count = (await db.execute(select(func.count(Article.id)))).scalar() or 0
    error_feed_count = (await db.execute(
        select(func.count(Feed.id)).where(Feed.status == "error")
    )).scalar() or 0
    recent_errors = (await db.execute(
        select(FetchLog)
        .options(selectinload(FetchLog.feed))
        .where(FetchLog.failed_at >= since)
        .order_by(FetchLog.failed_at.desc())
        .limit(5)
    )).scalars().all()
    readable_pending = (await db.execute(
        select(func.count(Article.id)).where(Article.readable_status == "pending")
    )).scalar() or 0
    readable_failed = (await db.execute(
        select(func.count(Article.id)).where(Article.readable_status == "failed")
    )).scalar() or 0
    readable_pending_recent = (await db.execute(
        select(Article)
        .options(selectinload(Article.feed))
        .where(Article.readable_status == "pending")
        .order_by(Article.readable_retries.desc(), Article.id.desc())
        .limit(10)
    )).scalars().all()
    readable_failed_recent = (await db.execute(
        select(Article)
        .options(selectinload(Article.feed))
        .where(Article.readable_status == "failed")
        .where(Article.readable_failed_at >= since)
        .order_by(Article.readable_failed_at.desc())
        .limit(5)
    )).scalars().all()
    briefing_errors = await list_briefing_errors(db)
    auto_profile_errors = await list_auto_profile_errors(db)
    redirect_conflicts_list = await list_redirect_conflicts(db)
    return {
        "user_count": user_count,
        "active_user_count": active_user_count,
        "feed_count": feed_count,
        "article_count": article_count,
        "error_feed_count": error_feed_count,
        "recent_errors": recent_errors,
        "readable_pending": readable_pending,
        "readable_failed": readable_failed,
        "readable_pending_recent": readable_pending_recent,
        "readable_failed_recent": readable_failed_recent,
        "briefing_errors": briefing_errors,
        "auto_profile_errors": auto_profile_errors,
        "redirect_conflicts": redirect_conflicts_list,
    }

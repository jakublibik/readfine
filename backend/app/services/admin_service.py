"""Admin service: user management, app settings, invitations, audit log."""
import logging
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import Article
from app.models.auth import Invitation
from app.models.feed import Feed, UserFeed
from app.models.fetch_log import FetchLog
from app.models.settings import AppSettings, AuditLog
from app.models.user import User

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
    feed_counts = (
        select(UserFeed.user_id, func.count(UserFeed.feed_id).label("feed_count"))
        .group_by(UserFeed.user_id)
        .subquery()
    )
    article_counts = (
        select(UserFeed.user_id, func.count(Article.id).label("article_count"))
        .join(Article, Article.feed_id == UserFeed.feed_id)
        .group_by(UserFeed.user_id)
        .subquery()
    )
    rows = (await db.execute(
        select(User, func.coalesce(feed_counts.c.feed_count, 0), func.coalesce(article_counts.c.article_count, 0))
        .outerjoin(feed_counts, feed_counts.c.user_id == User.id)
        .outerjoin(article_counts, article_counts.c.user_id == User.id)
        .order_by(User.created_at.desc())
    )).all()
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        user = row[0]
        if user.last_active_at:
            inactive_days = (now - user.last_active_at.replace(tzinfo=timezone.utc) if user.last_active_at.tzinfo is None else now - user.last_active_at).days
        else:
            inactive_days = None
        result.append({"user": user, "feed_count": row[1], "article_count": row[2], "inactive_days": inactive_days})
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
        token=secrets.token_urlsafe(32),
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


async def delete_feed(db: AsyncSession, feed_id: int) -> bool:
    feed = await db.get(Feed, feed_id)
    if not feed or feed.subscriber_count > 0:
        return False
    await db.execute(delete(Article).where(Article.feed_id == feed_id))
    await db.delete(feed)
    await db.commit()
    return True


async def list_feeds_with_stats(db: AsyncSession) -> list[dict]:
    article_counts = (
        select(Article.feed_id, func.count(Article.id).label("article_count"))
        .group_by(Article.feed_id)
        .subquery()
    )
    rows = (await db.execute(
        select(Feed, func.coalesce(article_counts.c.article_count, 0))
        .outerjoin(article_counts, article_counts.c.feed_id == Feed.id)
        .order_by(Feed.status.desc(), func.lower(Feed.title))
    )).all()
    return [{"feed": row[0], "article_count": row[1]} for row in rows]


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
    }

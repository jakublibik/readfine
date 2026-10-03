from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password
from app.models.user import User, UserSettings
from app.models.settings import AppSettings


async def seed_first_admin(db: AsyncSession, email: str, password: str) -> None:
    """Creates the first admin user if no users exist yet."""
    result = await db.execute(select(User).limit(1))
    if result.scalar_one_or_none():
        return

    admin = User(
        email=email,
        password_hash=hash_password(password),
        display_name="Admin",
        role="admin",
    )
    db.add(admin)
    await db.flush()

    db.add(UserSettings(user_id=admin.id))

    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    if not result.scalar_one_or_none():
        db.add(AppSettings(id=1))

    await db.commit()


# How stale last_active_at may get before a request writes it again. It only has to be
# good to the day (the admin table, the automatic profile's cutoff), and an hour keeps
# the write off almost every request.
LAST_ACTIVE_RESOLUTION = timedelta(hours=1)


async def record_activity(user: User, db: AsyncSession) -> bool:
    """Write last_active_at now. Returns True when this woke a dormant account that
    had feeds paused because of it (dormancy_service), which the web shows as a
    one-off banner. Asked before the write: afterwards the account is awake."""
    from app.services.dormancy_service import wakes_with_paused_feeds

    woke = await wakes_with_paused_feeds(user.id, db)
    user.last_active_at = datetime.now(timezone.utc)
    await db.commit()
    return woke


# Session key for the one-off "feeds were paused" banner; set on wake-up, read and
# dropped by the next full /app render.
FEEDS_RESUMED_SESSION_KEY = "feeds_resumed"


async def touch_last_active(user: User, db: AsyncSession, session: dict | None = None) -> bool:
    """Record that the reader is using the app, at most once per LAST_ACTIVE_RESOLUTION.

    Called from what a person does (opening the app, opening an article, scrolling
    articles read, changing state over the API), not from polling or listing, which an
    open tab or a sync client does on its own. The user row is loaded fresh on every
    request, so the check needs no cache. Two requests at once may both write; harmless.

    The hourly pass never hides a wake-up: a dormant account has been away for weeks.
    Web callers pass the *session* so a wake-up leaves the banner flag in it.
    """
    now = datetime.now(timezone.utc)
    if user.last_active_at and user.last_active_at >= now - LAST_ACTIVE_RESOLUTION:
        return False
    woke = await record_activity(user, db)
    if woke and session is not None:
        session[FEEDS_RESUMED_SESSION_KEY] = True
    return woke

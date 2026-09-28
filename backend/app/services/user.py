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


async def touch_last_active(user: User, db: AsyncSession) -> None:
    """Record that the reader is using the app, at most once per LAST_ACTIVE_RESOLUTION.

    Called from what a person does (opening the app, opening an article, scrolling
    articles read, changing state over the API), not from polling or listing, which an
    open tab or a sync client does on its own. The user row is loaded fresh on every
    request, so the check needs no cache. Two requests at once may both write; harmless.
    """
    now = datetime.now(timezone.utc)
    if user.last_active_at and user.last_active_at >= now - LAST_ACTIVE_RESOLUTION:
        return
    user.last_active_at = now
    await db.commit()

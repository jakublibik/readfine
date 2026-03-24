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

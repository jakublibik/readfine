"""Process-level cache for the public ``registration_enabled`` flag.

``AppSettings`` is a singleton row that changes only via the admin panel, yet the
public landing decision (``/``, ``/login``, ``/register``) reads it on every
unauthenticated hit — and ``/`` is the most crawled endpoint (bots). Cache only
the single boolean those public GET routes need; authenticated/admin code keeps
reading the full ``AppSettings`` fresh from the DB, so SMTP/AI/legal config never
goes stale. Invalidated on every admin write (see ``update_app_settings``).

Single-process assumption: the cache and its invalidation are per-process. The
target deployment runs one app process (APScheduler lives in-process too), so the
admin write that toggles registration invalidates the same process that serves the
public routes. Under a multi-worker deploy (e.g. several Uvicorn workers) only the
worker handling the admin save drops its copy; others keep the stale flag until
restart — the same caveat as the in-memory login rate limiter. The other
module-level mirrors (``set_ai_enabled`` / ``set_feedback_available``) share it.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import AppSettings

# None = not loaded yet; otherwise the cached registration_enabled value.
_cached_registration_enabled: bool | None = None


async def get_registration_enabled(db: AsyncSession) -> bool:
    """Return ``registration_enabled``, loading it once and caching in-process."""
    global _cached_registration_enabled
    if _cached_registration_enabled is None:
        result = await db.execute(
            select(AppSettings.registration_enabled).where(AppSettings.id == 1)
        )
        _cached_registration_enabled = bool(result.scalar_one_or_none())
    return _cached_registration_enabled


def invalidate_registration_cache() -> None:
    """Drop the cached flag; next read reloads from the DB."""
    global _cached_registration_enabled
    _cached_registration_enabled = None

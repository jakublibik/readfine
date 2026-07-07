"""Persistence bridge for the in-memory learned per-host fetch spacing.

``app.fetcher.host_throttle`` holds the authoritative, hot-path in-memory store and
tracks which hosts changed. This service hydrates that store at startup and flushes
changed hosts back to the ``host_rate_limits`` table, so learned spacing survives
restarts/deploys (otherwise an aggressive host is re-probed into a 429 each time).
"""
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.fetcher import host_throttle
from app.models.host_rate_limit import HostRateLimit


async def load_into_memory(db: AsyncSession) -> None:
    """Populate the in-memory spacing store from the DB (call once at startup)."""
    rows = (await db.execute(select(HostRateLimit))).scalars().all()
    host_throttle.load_spacing([
        host_throttle.LearnedSpacing(
            host=r.host,
            seconds=r.spacing_seconds,
            source=r.source,
            learned_at=r.learned_at,
            consecutive_429=r.consecutive_429,
        )
        for r in rows
    ])


async def flush(db: AsyncSession) -> None:
    """Write back every host that changed since the last flush: upsert present
    entries, delete ones an admin (or clear) removed. Batched into one commit."""
    hosts = host_throttle.drain_dirty()
    if not hosts:
        return
    for host in hosts:
        entry = host_throttle.get_spacing(host)
        if entry is None:
            await db.execute(delete(HostRateLimit).where(HostRateLimit.host == host))
        else:
            await db.execute(
                insert(HostRateLimit)
                .values(
                    host=entry.host,
                    spacing_seconds=entry.seconds,
                    source=entry.source,
                    consecutive_429=entry.consecutive_429,
                    learned_at=entry.learned_at,
                )
                .on_conflict_do_update(
                    index_elements=["host"],
                    set_=dict(
                        spacing_seconds=entry.seconds,
                        source=entry.source,
                        consecutive_429=entry.consecutive_429,
                        learned_at=entry.learned_at,
                    ),
                )
            )
    await db.commit()

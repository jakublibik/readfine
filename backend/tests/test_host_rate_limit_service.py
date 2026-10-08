"""Learned per-host spacing survives a restart: flush writes it, load reads it back."""
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.fetcher import host_throttle
from app.models.host_rate_limit import HostRateLimit
from app.services.host_rate_limit_service import flush, load_into_memory

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    session.commit = session.flush  # keep the rollback isolation
    host_throttle.clear()
    try:
        yield session
    finally:
        host_throttle.clear()
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _row(session, host):
    return await session.scalar(select(HostRateLimit).where(HostRateLimit.host == host))


def _learn(host):
    for _ in range(host_throttle.TIGHTEN_AFTER_429):
        entry = host_throttle.record_rate_limited(host, NOW, retry_after_seconds=30)
    assert entry.seconds > 0
    return entry


async def test_flush_then_load_restores_the_spacing(pg):
    host = f"{uuid.uuid4().hex}.example"
    learned = _learn(host)

    await flush(pg)
    row = await _row(pg, host)
    assert (row.spacing_seconds, row.source, row.consecutive_429) == (
        learned.seconds, "429", learned.consecutive_429,
    )

    host_throttle.clear()  # the restart
    await load_into_memory(pg)
    restored = host_throttle.get_spacing(host)
    assert (restored.seconds, restored.source, restored.consecutive_429) == (
        learned.seconds, learned.source, learned.consecutive_429,
    )
    assert host_throttle.drain_dirty() == set()  # a load is not a change


async def test_flush_updates_an_existing_row(pg):
    host = f"{uuid.uuid4().hex}.example"
    _learn(host)
    await flush(pg)
    tighter = host_throttle.record_rate_limited(host, NOW, retry_after_seconds=600)
    await flush(pg)
    await pg.refresh(await _row(pg, host))
    assert (await _row(pg, host)).spacing_seconds == tighter.seconds


async def test_cleared_host_is_deleted(pg):
    host = f"{uuid.uuid4().hex}.example"
    _learn(host)
    await flush(pg)
    assert host_throttle.clear_spacing(host)
    await flush(pg)
    assert await _row(pg, host) is None


async def test_nothing_dirty_writes_nothing(pg):
    await flush(pg)  # no error, no rows touched
    assert host_throttle.drain_dirty() == set()

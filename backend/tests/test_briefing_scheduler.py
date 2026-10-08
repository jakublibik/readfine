"""Scheduled briefings against the real (dev) DB: who still gets sent, and the cap.

Runs in a rolled-back transaction and skips if the DB is unreachable. Other due
briefings may exist in the dev DB; the assertions only look at the configs these
tests create.
"""
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.settings import AppSettings
from app.models.user import User, UserCatchupConfig


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
    session = AsyncSession(bind=conn, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _user(pg, *, active=True):
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}",
                is_active=active, email_verified=True)
    pg.add(user)
    await pg.flush()
    return user


async def _briefing(pg, user, *, enabled=True, due=True, recipients=None):
    cfg = UserCatchupConfig(
        briefing_recipients=json.dumps(recipients) if recipients else None,
        user_id=user.id, name=f"b-{uuid.uuid4().hex[:6]}", period="7days",
        filter_status="all", article_limit=50, include_snippet=False,
        briefing_enabled=enabled, briefing_interval="daily", briefing_time="08:00",
        briefing_next_send_at=(datetime.now(timezone.utc) - timedelta(minutes=5)) if due else None,
    )
    pg.add(cfg)
    await pg.flush()
    return cfg


async def test_deactivated_account_is_not_sent(pg):
    from app.fetcher import scheduler

    await pg.execute(update(AppSettings).where(AppSettings.id == 1).values(
        ai_enabled=True, smtp_host="smtp.ex.invalid", smtp_from_email="rf@ex.invalid",
    ))
    active = await _briefing(pg, await _user(pg))
    stopped = await _briefing(pg, await _user(pg, active=False))

    @asynccontextmanager
    async def factory():
        yield pg

    sent = AsyncMock()
    with (
        patch("app.database.async_session_factory", factory),
        patch("app.services.briefing_service.send_briefing", sent),
    ):
        await scheduler._send_due_briefings()

    sent_ids = {call.args[0].id for call in sent.await_args_list}
    assert active.id in sent_ids
    assert stopped.id not in sent_ids


async def test_cap_counts_only_switched_on_briefings_with_recipients(pg):
    from app.services.briefing_service import other_briefings_with_recipients

    user = await _user(pg)
    extra = ["friend@ex.invalid"]
    this = await _briefing(pg, user, recipients=extra)
    await _briefing(pg, user, recipients=extra)
    await _briefing(pg, user, recipients=extra)
    await _briefing(pg, user)  # to the owner only
    await _briefing(pg, user, enabled=False, due=False, recipients=extra)
    await _briefing(pg, await _user(pg), recipients=extra)  # someone else's

    assert await other_briefings_with_recipients(user.id, this.id, pg) == 2

"""Scope cleanup when a feed subscription or folder is deleted.

Stripping dangling ``feed:<id>`` / ``folder:<id>`` tokens from filter and
catchup/briefing scopes, with the two widening safeguards (deactivate a filter
whose include-scope empties; disable a briefing whose include-scope empties).

Pure-token tests need no DB; the rest run against the real (dev) DB in a
rolled-back transaction and skip if unreachable.
"""
import json
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.feed import Feed, Folder, UserFeed
from app.models.filter import Filter
from app.models.user import User, UserCatchupConfig
from app.services.feed import unsubscribe
from app.services.scope_cleanup import _strip, strip_scope_references


# ── _strip (pure) ─────────────────────────────────────────────────────────────

class TestStrip:
    def test_removes_token_keeps_rest(self):
        new, present, emptied = _strip('["feed:5", "feed:9"]', "feed:5")
        assert json.loads(new) == ["feed:9"]
        assert present is True and emptied is False

    def test_emptied_when_last_token_removed(self):
        new, present, emptied = _strip('["feed:5"]', "feed:5")
        assert new is None
        assert present is True and emptied is True

    def test_absent_token_is_noop(self):
        new, present, emptied = _strip('["feed:9"]', "feed:5")
        assert new == '["feed:9"]'
        assert present is False and emptied is False

    def test_prefix_not_confused(self):
        # feed:5 must not match feed:50
        new, present, emptied = _strip('["feed:50"]', "feed:5")
        assert present is False and emptied is False

    def test_none_and_empty(self):
        assert _strip(None, "feed:5") == (None, False, False)
        assert _strip("", "feed:5") == ("", False, False)

    def test_corrupt_left_untouched(self):
        new, present, emptied = _strip("not json", "feed:5")
        assert new == "not json"
        assert present is False and emptied is False


# ── DB fixtures ───────────────────────────────────────────────────────────────

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
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _user(session):
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}")
    session.add(user)
    await session.flush()
    return user


async def _filter(session, user, *, include=None, exceptt=None, name="f"):
    f = Filter(
        user_id=user.id,
        name=name,
        scope_include=json.dumps(include) if include else None,
        scope_except=json.dumps(exceptt) if exceptt else None,
    )
    session.add(f)
    await session.flush()
    return f


async def _config(session, user, *, include=None, briefing=False, name="c"):
    c = UserCatchupConfig(
        user_id=user.id,
        name=name,
        scope_include=json.dumps(include) if include else None,
        briefing_enabled=briefing,
        briefing_next_send_at=datetime.now(timezone.utc) if briefing else None,
    )
    session.add(c)
    await session.flush()
    return c


# ── strip_scope_references ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filter_narrowed_not_deactivated(pg):
    user = await _user(pg)
    f = await _filter(pg, user, include=["feed:5", "feed:9"])
    res = await strip_scope_references(pg, kind="feed", ref_id=5, user_id=user.id)
    await pg.flush()
    await pg.refresh(f)
    assert json.loads(f.scope_include) == ["feed:9"]
    assert f.is_active is True
    assert res.has_changes is False


@pytest.mark.asyncio
async def test_filter_emptied_is_deactivated_and_reported(pg):
    user = await _user(pg)
    f = await _filter(pg, user, include=["feed:5"], name="only-feed-5")
    res = await strip_scope_references(pg, kind="feed", ref_id=5, user_id=user.id)
    await pg.flush()
    await pg.refresh(f)
    assert f.scope_include is None
    assert f.is_active is False
    assert res.deactivated_filters == ["only-feed-5"]


@pytest.mark.asyncio
async def test_except_only_ref_cleared_without_deactivation(pg):
    user = await _user(pg)
    f = await _filter(pg, user, include=["feed:9"], exceptt=["feed:5"])
    res = await strip_scope_references(pg, kind="feed", ref_id=5, user_id=user.id)
    await pg.flush()
    await pg.refresh(f)
    assert f.scope_except is None
    assert f.is_active is True
    assert res.has_changes is False


@pytest.mark.asyncio
async def test_plain_catchup_emptied_silently(pg):
    user = await _user(pg)
    c = await _config(pg, user, include=["feed:5"], briefing=False)
    res = await strip_scope_references(pg, kind="feed", ref_id=5, user_id=user.id)
    await pg.flush()
    await pg.refresh(c)
    assert c.scope_include is None
    assert res.has_changes is False


@pytest.mark.asyncio
async def test_briefing_emptied_is_disabled_and_reported(pg):
    user = await _user(pg)
    c = await _config(pg, user, include=["feed:5"], briefing=True, name="daily")
    res = await strip_scope_references(pg, kind="feed", ref_id=5, user_id=user.id)
    await pg.flush()
    await pg.refresh(c)
    assert c.scope_include is None
    assert c.briefing_enabled is False
    assert c.briefing_next_send_at is None
    assert res.disabled_briefings == ["daily"]


@pytest.mark.asyncio
async def test_folder_token(pg):
    user = await _user(pg)
    f = await _filter(pg, user, include=["folder:3", "feed:9"])
    await strip_scope_references(pg, kind="folder", ref_id=3, user_id=user.id)
    await pg.flush()
    await pg.refresh(f)
    assert json.loads(f.scope_include) == ["feed:9"]


@pytest.mark.asyncio
async def test_scoped_to_single_user(pg):
    a, b = await _user(pg), await _user(pg)
    fa = await _filter(pg, a, include=["feed:5"], name="a")
    fb = await _filter(pg, b, include=["feed:5"], name="b")
    await strip_scope_references(pg, kind="feed", ref_id=5, user_id=a.id)
    await pg.flush()
    await pg.refresh(fa)
    await pg.flush()
    await pg.refresh(fb)
    assert fa.is_active is False
    assert fb.is_active is True  # other user's filter untouched


@pytest.mark.asyncio
async def test_all_users_when_user_id_none(pg):
    a, b = await _user(pg), await _user(pg)
    fa = await _filter(pg, a, include=["feed:5"], name="a")
    fb = await _filter(pg, b, include=["feed:5"], name="b")
    await strip_scope_references(pg, kind="feed", ref_id=5, user_id=None)
    await pg.flush()
    await pg.refresh(fa)
    await pg.flush()
    await pg.refresh(fb)
    assert fa.is_active is False and fb.is_active is False


# ── integration through unsubscribe ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_unsubscribe_strips_and_reports(pg):
    user = await _user(pg)
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    pg.add(feed)
    await pg.flush()
    uf = UserFeed(user_id=user.id, feed_id=feed.id)
    pg.add(uf)
    await pg.flush()
    f = await _filter(pg, user, include=[f"feed:{feed.id}"], name="scoped")

    res = await unsubscribe(user, uf.id, pg)

    await pg.flush()

    await pg.refresh(f)
    assert f.is_active is False
    assert res.deactivated_filters == ["scoped"]

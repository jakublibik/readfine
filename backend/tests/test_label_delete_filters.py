"""Deleting a label takes its "add label" filter actions with it (real Postgres)."""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.filter import Filter, FilterAction
from app.models.label import Label
from app.models.user import User
from app.services.label_service import delete_label

pytestmark = pytest.mark.asyncio


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


async def _filter(session, user, name, actions):
    f = Filter(user_id=user.id, name=name, is_active=True)
    f.actions = [FilterAction(action_type=t, action_value=v) for t, v in actions]
    session.add(f)
    await session.flush()
    return f


async def _actions(session, f):
    return sorted(
        (a.action_type, a.action_value) for a in (await session.execute(
            select(FilterAction).where(FilterAction.filter_id == f.id)
        )).scalars()
    )


async def test_label_action_removed_and_lone_filter_switched_off(pg):
    user = await _user(pg)
    gone, keep = Label(user_id=user.id, name="gone"), Label(user_id=user.id, name="keep")
    pg.add_all([gone, keep])
    await pg.flush()
    only = await _filter(pg, user, "only-label", [("label", str(gone.id))])
    mixed = await _filter(pg, user, "mixed", [("label", str(gone.id)), ("star", None)])
    other = await _filter(pg, user, "other", [("label", str(keep.id))])

    switched_off = await delete_label(user, gone.id, pg)

    assert switched_off == ["only-label"]
    for f in (only, mixed, other):
        await pg.refresh(f)
    assert await _actions(pg, only) == []
    assert only.is_active is False
    assert await _actions(pg, mixed) == [("star", None)]
    assert mixed.is_active is True
    assert await _actions(pg, other) == [("label", str(keep.id))]
    assert other.is_active is True


async def test_other_users_filters_untouched(pg):
    owner, stranger = await _user(pg), await _user(pg)
    label = Label(user_id=owner.id, name="l")
    pg.add(label)
    await pg.flush()
    # A hand-made action on someone else's filter pointing at the same id.
    theirs = await _filter(pg, stranger, "theirs", [("label", str(label.id))])

    assert await delete_label(owner, label.id, pg) == []
    assert await _actions(pg, theirs) == [("label", str(label.id))]


async def test_not_found_is_none(pg):
    user = await _user(pg)
    assert await delete_label(user, 2_000_000_000, pg) is None

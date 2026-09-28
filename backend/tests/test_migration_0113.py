"""Migration 0113 rewrites a score condition "gt N" as "gte N + 1", which the filter
compares the same way, and leaves every other condition alone.

Runs the migration's SQL against the real (dev) DB in a rolled-back transaction and
skips if unreachable.
"""
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.filter import Filter, FilterCondition
from app.models.user import User

_PATH = Path(__file__).parent.parent / "alembic" / "versions" / "0113_score_conditions_gte.py"


def _migration():
    spec = importlib.util.spec_from_file_location("m0113", _PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


async def _run(pg, direction):
    statements = []
    with patch.object(_migration_mod.op, "execute", side_effect=statements.append):
        getattr(_migration_mod, direction)()
    for sql in statements:
        await pg.execute(text(sql))
    pg.expire_all()


_migration_mod = _migration()


async def _conditions(pg, *rows):
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}")
    pg.add(user)
    await pg.flush()
    f = Filter(user_id=user.id, name="f")
    pg.add(f)
    await pg.flush()
    conds = [FilterCondition(filter_id=f.id, field=field, operator=op, value=val, position=i)
             for i, (field, op, val) in enumerate(rows)]
    pg.add_all(conds)
    await pg.flush()
    return [c.id for c in conds]


async def _read(pg, ids):
    rows = (await pg.execute(
        select(FilterCondition.id, FilterCondition.operator, FilterCondition.value)
        .where(FilterCondition.id.in_(ids)))).all()
    by_id = {r.id: (r.operator, r.value) for r in rows}
    return [by_id[i] for i in ids]


async def test_upgrade_rewrites_only_score_gt(pg):
    ids = await _conditions(
        pg,
        ("relevance_score", "gt", "74"),
        ("ai_score", "gt", "59.5"),
        ("basic_score", "gt", "100"),         # can never match, stays
        ("basic_score", "lt", "30"),
        ("ai_score", "equals", "50"),
        ("published_at", "gt", "2026-01-01"),
    )
    await _run(pg, "upgrade")
    assert await _read(pg, ids) == [
        ("gte", "75"), ("gte", "60"), ("gt", "100"),
        ("lt", "30"), ("equals", "50"), ("gt", "2026-01-01"),
    ]


async def test_downgrade_restores_gt(pg):
    ids = await _conditions(pg, ("relevance_score", "gte", "75"))
    await _run(pg, "downgrade")
    assert await _read(pg, ids) == [("gt", "74")]

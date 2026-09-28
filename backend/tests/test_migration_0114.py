"""Migration 0114 decodes the HTML entities stored in article titles and leaves
every other title alone.

Runs the migration against the real (dev) DB in a rolled-back transaction and skips
if unreachable.
"""
import importlib.util
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed

_PATH = Path(__file__).parent.parent / "alembic" / "versions" / "0114_article_titles_unescape.py"
_spec = importlib.util.spec_from_file_location("m0114", _PATH)
m0114 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m0114)


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
        yield session, conn
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def test_entities_in_titles_are_decoded(pg):
    session, conn = pg
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="f", subscriber_count=1)
    session.add(feed)
    await session.flush()
    titles = [
        "Can an &#8216;eSUV&#8217; e-bike",
        "Tom &amp;amp; Jerry",           # encoded twice
        "R&D at AT&T",                   # no entity, untouched
        "Plain title",
    ]
    ids = []
    for t in titles:
        g = uuid.uuid4().hex
        a = Article(feed_id=feed.id, guid=g, guid_hash=g, title=t, content="x")
        session.add(a)
        await session.flush()
        ids.append(a.id)

    # The migration runs on a sync connection through op.get_bind().
    await conn.run_sync(lambda sync_conn: _upgrade_on(sync_conn))
    session.expire_all()

    rows = dict((await session.execute(
        select(Article.id, Article.title).where(Article.id.in_(ids)))).all())
    assert [rows[i] for i in ids] == [
        "Can an \u2018eSUV\u2019 e-bike", "Tom & Jerry", "R&D at AT&T", "Plain title",
    ]


def _upgrade_on(sync_conn):
    with patch.object(m0114.op, "get_bind", return_value=sync_conn):
        m0114.upgrade()

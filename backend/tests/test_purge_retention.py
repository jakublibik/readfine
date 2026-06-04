"""Integration tests for tiered retention purge (engaged-protection, trim, T2).

Runs against the real (dev) database inside a transaction that is always rolled
back, and scopes every mutation to a throwaway test feed so existing data is never
touched. Skips automatically if the database is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.ai_service import PROFILE_MAX_WINDOW_DAYS
from app.services.purge_service import (
    _engaged_exists,
    _fully_protected_exists,
    _trim_engaged,
)

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception:
        await engine.dispose()
        pytest.skip("database not reachable")
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _setup(session) -> tuple[User, Feed]:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"purge_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return user, feed


async def _article(session, feed, *, age_days, content="<p>raw</p>", readable=None,
                   status="success", trimmed_at=None) -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title="T",
        content=content, readable_content=readable, readable_status=status,
        fetched_at=NOW - timedelta(days=age_days), trimmed_at=trimmed_at,
    )
    session.add(a)
    await session.flush()
    return a


async def _state(session, user, article, *, dwell=0, opened=False, ever_starred=False,
                 starred=False, archived=False, share=None, created_days=0) -> UserArticleState:
    st = UserArticleState(
        user_id=user.id, article_id=article.id, dwell_seconds=dwell, link_opened=opened,
        ever_starred=ever_starred, is_starred=starred, is_archived=archived, share_token=share,
        created_at=NOW - timedelta(days=created_days),
    )
    session.add(st)
    await session.flush()
    return st


async def _exists(session, article_id) -> bool:
    return (await session.execute(
        select(Article.id).where(Article.id == article_id)
    )).first() is not None


async def _delete_unengaged(session, feed_id, cutoff) -> int:
    res = await session.execute(
        delete(Article).where(
            Article.feed_id == feed_id,
            Article.fetched_at < cutoff,
            ~_fully_protected_exists(),
            ~_engaged_exists(),
        )
    )
    return res.rowcount


CUTOFF = NOW - timedelta(days=60)


# ── DELETE pass (unengaged) ──────────────────────────────────────────────────

class TestAgeDelete:
    async def test_unengaged_old_deleted(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100)
        await _state(pg, user, a, dwell=0)  # not engaged
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 1
        assert not await _exists(pg, a.id)

    async def test_no_state_old_deleted(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100)  # no uas at all
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 1
        assert not await _exists(pg, a.id)

    async def test_engaged_not_deleted(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100)
        await _state(pg, user, a, dwell=120)  # engaged → protected from delete
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 0
        assert await _exists(pg, a.id)

    async def test_recent_unengaged_kept(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=10)
        await _state(pg, user, a, dwell=0)
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 0
        assert await _exists(pg, a.id)


# ── TRIM pass (engaged) ──────────────────────────────────────────────────────

class TestTrim:
    async def test_engaged_old_trimmed_readable(self, pg):
        user, feed = await _setup(pg)
        long_readable = "<p>" + ("word " * 500) + "</p>"
        a = await _article(pg, feed, age_days=100, content="<p>raw body</p>", readable=long_readable)
        await _state(pg, user, a, dwell=120, share="tok-abc")

        n = await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW)
        assert n == 1

        row = (await pg.execute(
            select(Article.content, Article.readable_content, Article.trimmed_at)
            .where(Article.id == a.id)
        )).one()
        assert row.content is None                      # raw body dropped
        assert row.trimmed_at is not None
        assert 0 < len(row.readable_content) <= 300     # readable trimmed to snippet
        # share revoked
        token = (await pg.execute(
            select(UserArticleState.share_token).where(UserArticleState.article_id == a.id)
        )).scalar_one()
        assert token is None

    async def test_content_only_trimmed_in_place(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100, content="<p>" + ("x " * 500) + "</p>", readable=None)
        await _state(pg, user, a, dwell=120)
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 1
        row = (await pg.execute(
            select(Article.content, Article.readable_content).where(Article.id == a.id)
        )).one()
        assert row.readable_content is None
        assert 0 < len(row.content) <= 300

    async def test_fully_protected_not_trimmed(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100, readable="<p>" + ("w " * 500) + "</p>")
        await _state(pg, user, a, dwell=120, starred=True)  # read AND starred → keep full
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 0
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 0
        assert await _exists(pg, a.id)

    async def test_recent_engaged_not_trimmed(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=10, readable="<p>body</p>")
        await _state(pg, user, a, dwell=120)
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 0

    async def test_trim_idempotent(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100, readable="<p>" + ("w " * 500) + "</p>")
        await _state(pg, user, a, dwell=120)
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 1
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 0

    async def test_link_opened_counts_as_engaged(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100, readable="<p>" + ("w " * 500) + "</p>")
        await _state(pg, user, a, dwell=0, opened=True)  # opened link → engaged
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 1


# ── T2 delete (trimmed stubs past the profile window) ────────────────────────

class TestT2Delete:
    def _t2_delete(self, session, feed_id):
        cutoff_t2 = NOW - timedelta(days=PROFILE_MAX_WINDOW_DAYS)
        recent = (
            select(UserArticleState.article_id)
            .where(
                UserArticleState.article_id == Article.id,
                UserArticleState.created_at >= cutoff_t2,
            )
            .exists()
        )
        return session.execute(
            delete(Article).where(
                Article.feed_id == feed_id,
                Article.trimmed_at.isnot(None),
                ~recent,
                ~_fully_protected_exists(),
            )
        )

    async def test_old_stub_deleted(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=200, readable="snip", trimmed_at=NOW - timedelta(days=130))
        await _state(pg, user, a, dwell=120, created_days=200)  # engaged 200d ago > 180
        res = await self._t2_delete(pg, feed.id)
        assert res.rowcount == 1
        assert not await _exists(pg, a.id)

    async def test_stub_with_recent_signal_kept(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=200, readable="snip", trimmed_at=NOW - timedelta(days=130))
        await _state(pg, user, a, dwell=120, created_days=90)  # signal within 180d → keep
        res = await self._t2_delete(pg, feed.id)
        assert res.rowcount == 0
        assert await _exists(pg, a.id)

    async def test_untrimmed_not_touched_by_t2(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=200, readable="full", trimmed_at=None)
        await _state(pg, user, a, dwell=120, created_days=200)
        res = await self._t2_delete(pg, feed.id)
        assert res.rowcount == 0
        assert await _exists(pg, a.id)


# ── visibility: trimmed stubs are hidden from listings ───────────────────────

class TestHidden:
    async def test_list_articles_excludes_trimmed(self, pg):
        from app.services.article import list_articles
        user, feed = await _setup(pg)
        normal = await _article(pg, feed, age_days=5, content="<p>normal</p>")
        trimmed = await _article(pg, feed, age_days=100, content="snip", trimmed_at=NOW)
        items = await list_articles(user, pg, feed_id=feed.id, limit=50)
        ids = {it.id for it in items}
        assert normal.id in ids
        assert trimmed.id not in ids


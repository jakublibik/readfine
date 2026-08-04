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
                   status="success", trimmed_at=None, published_days=None) -> Article:
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title="T",
        content=content, readable_content=readable, readable_status=status,
        fetched_at=NOW - timedelta(days=age_days), trimmed_at=trimmed_at,
        published_at=None if published_days is None else NOW - timedelta(days=published_days),
    )
    session.add(a)
    await session.flush()
    return a


async def _state(session, user, article, *, dwell=0, opened=False, ever_starred=False,
                 starred=False, archived=False, share=None, created_days=0,
                 saved=False) -> UserArticleState:
    st = UserArticleState(
        user_id=user.id, article_id=article.id, dwell_seconds=dwell, link_opened=opened,
        ever_starred=ever_starred, is_starred=starred, is_archived=archived, share_token=share,
        created_at=NOW - timedelta(days=created_days),
        saved_at=NOW - timedelta(days=created_days) if saved else None,
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

    async def test_saved_not_trimmed_or_deleted(self, pg):
        """A saved-by-URL article is kept in full forever — no TTL, no cap. Saved must
        not be the one place where content quietly expires after the most explicit
        action a reader can take."""
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100, readable="<p>" + ("w " * 500) + "</p>")
        await _state(pg, user, a, dwell=120, saved=True)
        assert await _trim_engaged(pg, feed_id=feed.id, orphan=False, cutoff=CUTOFF, now=NOW) == 0
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 0
        assert await _exists(pg, a.id)

    async def test_unsaving_re_exposes_the_article_to_purge(self, pg):
        user, feed = await _setup(pg)
        a = await _article(pg, feed, age_days=100, readable="<p>" + ("w " * 500) + "</p>")
        st = await _state(pg, user, a, dwell=0, saved=True)
        st.saved_at = None
        await pg.flush()
        assert await _delete_unengaged(pg, feed.id, CUTOFF) == 1

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


# ── full orchestration: purge_old_articles() end-to-end ──────────────────────

async def _set_globals_null(session) -> None:
    """Disable global age/count purge so only per-feed overrides act — keeps the
    orchestration deterministic and scoped to this test's feeds. Upserts id=1 so it
    also works on a fresh DB."""
    from app.models.settings import AppSettings
    s = await session.get(AppSettings, 1)
    if s is None:
        s = AppSettings(id=1, default_purge_after_days=None, default_purge_keep_count=None)
        session.add(s)
    else:
        s.default_purge_after_days = None
        s.default_purge_keep_count = None
    await session.flush()


class TestPurgeOrchestration:
    async def test_full_flow(self, pg):
        from app.services.purge_service import purge_old_articles

        await _set_globals_null(pg)

        # Feed A: per-feed age horizon of 30 days (age pass only).
        user, feed_a = await _setup(pg)
        uf_a = (await pg.execute(
            select(UserFeed).where(UserFeed.feed_id == feed_a.id, UserFeed.user_id == user.id)
        )).scalar_one()
        uf_a.purge_after_days = 30
        await pg.flush()

        old_unengaged = await _article(pg, feed_a, age_days=40)            # age-DELETE
        old_engaged = await _article(pg, feed_a, age_days=40, readable="full")
        await _state(pg, user, old_engaged, dwell=120, created_days=40)    # age-TRIM
        recent = await _article(pg, feed_a, age_days=5)                    # KEEP

        # Feed B: per-feed keep_count of 2, all recent so only the count pass acts.
        feed_b = Feed(feed_url=f"https://ex.invalid/{uuid.uuid4().hex}.xml", title="b", subscriber_count=1)
        pg.add(feed_b)
        await pg.flush()
        uf_b = UserFeed(user_id=user.id, feed_id=feed_b.id, purge_keep_count=2)
        pg.add(uf_b)
        await pg.flush()
        b_newest = await _article(pg, feed_b, age_days=5)
        b_mid = await _article(pg, feed_b, age_days=6)
        b_oldest = await _article(pg, feed_b, age_days=7)                  # count-DELETE (rn=3)

        # T2: a trimmed stub past the profile window with no recent state.
        stub = await _article(pg, feed_a, age_days=PROFILE_MAX_WINDOW_DAYS + 30,
                              content="snip", trimmed_at=NOW - timedelta(days=PROFILE_MAX_WINDOW_DAYS + 5))

        # Monkeypatch commit→flush so the final commit doesn't break rollback isolation.
        pg.commit = pg.flush

        total = await purge_old_articles(pg)

        # Age pass
        assert not await _exists(pg, old_unengaged.id)
        assert await _exists(pg, recent.id)
        # Engaged old article is trimmed in place, not deleted
        assert await _exists(pg, old_engaged.id)
        refreshed = await pg.get(Article, old_engaged.id)
        await pg.refresh(refreshed)
        assert refreshed.trimmed_at is not None
        # readable_content keeps a short snippet; raw content is dropped
        assert refreshed.content is None
        # Count pass: oldest excess beyond keep_count=2 deleted, the two newest kept
        assert await _exists(pg, b_newest.id)
        assert await _exists(pg, b_mid.id)
        assert not await _exists(pg, b_oldest.id)
        # T2: trimmed stub past window deleted
        assert not await _exists(pg, stub.id)
        # Return is total deletions; ≥3 from our fixtures (T2 may also drop other
        # pre-existing stubs in a shared dev DB, so don't assert an exact total).
        assert total >= 3


class TestCountOrdering:
    async def test_count_pass_orders_by_published_at(self, pg):
        """The count pass ranks by coalesce(published_at, fetched_at), so the
        deleted 'excess' is the oldest by publication date even when fetched_at
        disagrees. Guards the ORDER BY that the (now removed) ids_exceeding_count
        helper used to cover in isolation."""
        from app.services.purge_service import purge_old_articles

        await _set_globals_null(pg)
        user, feed = await _setup(pg)
        uf = (await pg.execute(
            select(UserFeed).where(UserFeed.feed_id == feed.id, UserFeed.user_id == user.id)
        )).scalar_one()
        uf.purge_keep_count = 2
        await pg.flush()

        # fetched_at order (newest→oldest): a, b, c
        # published_at order (newest→oldest): c, b, a  ← reversed
        # With keep_count=2 the count pass must delete `a` (oldest *published*),
        # not `c` (oldest *fetched*).
        a = await _article(pg, feed, age_days=1, published_days=9)   # excess by published_at
        b = await _article(pg, feed, age_days=2, published_days=6)
        c = await _article(pg, feed, age_days=3, published_days=3)   # kept despite oldest fetch

        pg.commit = pg.flush
        await purge_old_articles(pg)

        assert not await _exists(pg, a.id)
        assert await _exists(pg, b.id)
        assert await _exists(pg, c.id)


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


"""OPML import: how many feeds one import may try, and surviving a failed one.

Every new feed in a file is fetched inside the request, and only the ones that are
added count toward the feed limit. The attempt budget keeps a file of dead addresses
from holding the request for hours. A feed that fails after a write must not leave
the session unusable for the rest of the file.

The budget tests use a stub session; the rollback and race tests run against the
real (dev) DB in a rolled-back transaction.
"""
import uuid
from types import SimpleNamespace

import feedparser
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.feed import Feed, Folder, UserFeed
from app.models.user import User
from app.services.feed import subscribe
from app.services.opml import _ADMIN_FEED_TRIES, _MIN_FAILED_TRIES, import_opml


# ── Attempt budget (stub session) ─────────────────────────────────────────────

class _Rows:
    def __init__(self, values=()):
        self._values = list(values)

    def all(self):
        return []

    def scalars(self):
        return iter(self._values)

    def scalar_one_or_none(self):
        return None


class _StubDB:
    """Answers the queries the feeds-only import runs: the account's subscribed
    URLs and its feed limit."""

    def __init__(self, max_feeds=None, subscribed=()):
        self.max_feeds = max_feeds
        self.subscribed = subscribed

    async def execute(self, statement, *args, **kwargs):
        sql = str(statement)
        if "feeds.feed_url" in sql and "user_feeds" in sql:
            return _Rows(self.subscribed)
        return _Rows()

    async def scalar(self, statement, *args, **kwargs):
        return self.max_feeds if "max_feeds_per_user" in str(statement) else None

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def refresh(self, obj):
        pass

    async def flush(self):
        pass

    def add(self, obj):
        pass


def _opml(urls) -> bytes:
    outlines = "".join(f'<outline type="rss" text="F" xmlUrl="{u}"/>' for u in urls)
    return f'<opml version="2.0"><body>{outlines}</body></opml>'.encode()


async def _run(db, xml, role="user"):
    return await import_opml(
        user=SimpleNamespace(id=1, role=role), xml_bytes=xml,
        import_feeds=True, import_labels=False, import_prefs=False,
        import_filters=False, db=db,
    )


def _failing_subscribe(calls):
    async def fake(**kwargs):
        calls.append(kwargs["url"])
        raise ValueError("unreachable")
    return fake


async def test_dead_feeds_stop_at_free_slots_plus_minimum(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.services.opml.subscribe", _failing_subscribe(calls))

    urls = [f"https://dead{i}.invalid/rss" for i in range(60)]
    result = await _run(_StubDB(max_feeds=10, subscribed=["https://have.example/rss"]), _opml(urls))

    # 9 free slots, and at least _MIN_FAILED_TRIES failures on top of them.
    budget = 9 + _MIN_FAILED_TRIES
    assert len(calls) == budget
    assert result.feeds_failed == budget
    assert result.feeds_not_tried == 60 - budget
    assert result.feeds_over_limit == 0


async def test_budget_grows_with_the_limit(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.services.opml.subscribe", _failing_subscribe(calls))

    urls = [f"https://dead{i}.invalid/rss" for i in range(150)]
    result = await _run(_StubDB(max_feeds=50), _opml(urls))

    assert len(calls) == 100  # 50 free slots, 50 more for failures
    assert result.feeds_not_tried == 50


async def test_admin_has_a_fixed_ceiling(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.services.opml.subscribe", _failing_subscribe(calls))

    urls = [f"https://dead{i}.invalid/rss" for i in range(_ADMIN_FEED_TRIES + 5)]
    result = await _run(_StubDB(max_feeds=10), _opml(urls), role="admin")

    assert len(calls) == _ADMIN_FEED_TRIES
    assert result.feeds_not_tried == 5


async def test_feeds_already_subscribed_are_skipped_without_a_try(monkeypatch):
    calls: list[str] = []

    async def fake(**kwargs):
        calls.append(kwargs["url"])
        return SimpleNamespace(feed_id=len(calls))

    monkeypatch.setattr("app.services.opml.subscribe", fake)

    have = [f"https://have{i}.example/rss" for i in range(40)]
    new = "https://new.example/rss"
    # A limit of 40 leaves no free slot, so only the minimum budget is there; the 40
    # feeds the account already has must not use it up.
    result = await _run(_StubDB(max_feeds=40, subscribed=have), _opml(have + [new, new]))

    # The duplicate in the file is skipped once the first copy is in.
    assert calls == [new]
    assert result.feeds_added == 1
    assert result.feeds_skipped == 41
    assert result.feeds_not_tried == 0


async def test_file_within_budget_is_not_marked_stopped(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr("app.services.opml.subscribe", _failing_subscribe(calls))

    result = await _run(_StubDB(max_feeds=200), _opml([f"https://d{i}.invalid/" for i in range(5)]))

    assert result.feeds_failed == 5
    assert result.feeds_not_tried == 0


# ── Real DB ───────────────────────────────────────────────────────────────────

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
    # Savepoints, so the code's own commits and rollbacks stay inside the test's
    # transaction and are undone with it.
    session = AsyncSession(
        bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint",
    )
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _user(pg):
    user = User(email=f"opml_{uuid.uuid4().hex[:12]}@test.invalid",
                password_hash="x", display_name="t")
    pg.add(user)
    await pg.commit()
    return user


async def test_failure_after_a_write_does_not_sink_the_rest(pg, monkeypatch):
    """The first feed breaks the transaction mid-write; the next must still go in,
    and the folder both were filed under must survive the rollback."""
    user = await _user(pg)
    taken = f"https://ex.invalid/{uuid.uuid4().hex}.xml"
    pg.add(Feed(feed_url=taken, title="t", subscriber_count=0))
    await pg.commit()
    calls = {"n": 0}

    async def fake(*, db, folder_id, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            db.add(Feed(feed_url=taken, title="dup", subscriber_count=0))
            await db.flush()  # unique violation: the session now needs a rollback
        feed = Feed(feed_url=f"https://ex.invalid/{uuid.uuid4().hex}.xml",
                    title="ok", subscriber_count=0)
        db.add(feed)
        await db.flush()
        uf = UserFeed(user_id=user.id, feed_id=feed.id, folder_id=folder_id)
        db.add(uf)
        await db.commit()
        return uf

    monkeypatch.setattr("app.services.opml.subscribe", fake)
    xml = (
        '<opml version="2.0"><body><outline text="Imported">'
        '<outline xmlUrl="https://a.invalid/rss"/><outline xmlUrl="https://b.invalid/rss"/>'
        '</outline></body></opml>'
    ).encode()

    result = await import_opml(
        user=user, xml_bytes=xml, import_feeds=True, import_labels=False,
        import_prefs=False, import_filters=False, db=pg,
    )

    assert result.feeds_failed == 1
    assert result.feeds_added == 1
    folder = await pg.scalar(select(Folder).where(Folder.user_id == user.id))
    assert folder is not None and folder.name == "Imported"
    filed = await pg.scalar(select(UserFeed).where(UserFeed.user_id == user.id))
    assert filed.folder_id == folder.id


async def test_subscribe_joins_a_feed_created_while_it_was_fetching(pg, monkeypatch):
    """Two subscribes to the same new public feed: the one that loses the insert
    attaches to the winner's row instead of failing with a broken session."""
    user = await _user(pg)
    url = f"https://ex.invalid/{uuid.uuid4().hex}.xml"
    parsed = feedparser.parse(
        "<rss><channel><title>T</title><item><title>a</title>"
        "<link>https://ex.invalid/a</link><description>x</description></item>"
        "</channel></rss>"
    )

    async def no_dns(_url):
        return None

    async def fetch_while_someone_else_inserts(_url, auth=None):
        # The other request finishes first and creates the row.
        await pg.execute(text(
            "INSERT INTO feeds (feed_url, title, subscriber_count, is_private) "
            "VALUES (:u, 'other', 0, false)"
        ), {"u": url})
        return parsed, None

    monkeypatch.setattr("app.services.feed.async_validate_feed_url", no_dns)
    monkeypatch.setattr("app.services.feed.fetch_and_parse_url", fetch_while_someone_else_inserts)
    monkeypatch.setattr("app.services.feed.get_cached_feed_preview", lambda _u: None)
    monkeypatch.setattr("app.services.feed.get_cached_permanent_url", lambda _u: None)

    uf = await subscribe(
        user=user, url=url, folder_id=None, custom_title=None,
        fetch_auth_user=None, fetch_auth_pass=None, db=pg,
        trigger_initial_fetch=False,
    )

    rows = (await pg.execute(select(Feed).where(Feed.feed_url == url))).scalars().all()
    assert len(rows) == 1
    assert uf.feed_id == rows[0].id

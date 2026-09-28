"""The two-step welcome: topics, then where the articles come from.

The account counts as onboarded only after step 2, and the starter feeds go into
one folder per chosen topic. subscribe() itself is replaced here: what it does with
a URL is covered by the feed tests, this is about the flow around it.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.feed import Folder
from app.models.user import User, UserSettings
from app.services import starter_feeds_service as starter
from app.services.feed import AlreadySubscribed, FeedLimitReached
from app.services.starter_feeds_service import (
    StarterCategory,
    StarterFeed,
    load_starter_categories,
    preselected,
)


def _cat(id_, name, keywords, *urls):
    from app.services.relevance_service import tokenize
    return StarterCategory(
        id=id_, name=name,
        keywords=frozenset(t for k in keywords for t in tokenize(k)),
        feeds=tuple(StarterFeed(title=u.rsplit("/", 1)[-1], url=u) for u in urls),
    )


CATS = (
    _cat("tech", "Technology", ["software", "linux"], "https://ex.invalid/a", "https://ex.invalid/b"),
    _cat("food", "Food", ["baking", "sourdough"], "https://ex.invalid/c"),
)


# ── the shipped list and pre-checking (pure) ─────────────────────────────────

def test_shipped_list_loads_with_unique_ids_and_feeds():
    cats = load_starter_categories()
    assert cats
    assert len({c.id for c in cats}) == len(cats)
    assert all(c.feeds for c in cats)
    urls = [f.url for c in cats for f in c.feeds]
    assert len(set(urls)) == len(urls)
    assert all(u.startswith("https://") for u in urls)


def test_a_broken_list_offers_no_starter_feeds_instead_of_failing(tmp_path, monkeypatch):
    bad = tmp_path / "starter_feeds.yml"
    bad.write_text("categories:\n  - id: x\n    feeds: [unclosed\n", encoding="utf-8")
    monkeypatch.setattr(starter, "_PATH", bad)
    load_starter_categories.cache_clear()
    try:
        assert load_starter_categories() == ()
        monkeypatch.setattr(starter, "_PATH", tmp_path / "missing.yml")
        load_starter_categories.cache_clear()
        assert load_starter_categories() == ()
    finally:
        load_starter_categories.cache_clear()


def test_preselected_matches_whole_words_of_the_topics():
    assert preselected(CATS, "Sourdough baking, Formula 1") == {"food"}
    assert preselected(CATS, "Linux kernel\nsourdough") == {"tech", "food"}
    # A word inside another is not a match, and no topics check nothing.
    assert preselected(CATS, "softwarecraft") == set()
    assert preselected(CATS, None) == set()


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
    session = AsyncSession(bind=conn, expire_on_commit=False,
                           join_transaction_mode="create_savepoint")
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


@pytest.fixture
def fake_subscribe(monkeypatch):
    """Record subscribe() calls; a URL listed in `outcomes` raises what it maps to."""
    calls: list[tuple[str, int, str]] = []
    outcomes: dict[str, Exception] = {}

    async def _subscribe(user, url, folder_id, custom_title, db, **kw):
        if url in outcomes:
            raise outcomes[url]
        calls.append((url, folder_id, custom_title))

    monkeypatch.setattr(starter, "subscribe", _subscribe)
    monkeypatch.setattr(starter, "load_starter_categories", lambda: CATS)
    return calls, outcomes


async def _folders(session, user):
    rows = await session.execute(select(Folder.id, Folder.name).where(Folder.user_id == user.id))
    return {name: fid for fid, name in rows.all()}


async def _onboarded_at(session, user):
    return await session.scalar(
        select(UserSettings.onboarded_at).where(UserSettings.user_id == user.id)
    )


# ── subscribe_starter ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_starter_feeds_go_into_a_folder_per_topic(pg, fake_subscribe):
    calls, _ = fake_subscribe
    user = await _user(pg)
    result = await starter.subscribe_starter(user, ["tech", "food", "bogus"], pg)
    folders = await _folders(pg, user)
    assert set(folders) == {"Technology", "Food"}
    assert calls == [
        ("https://ex.invalid/a", folders["Technology"], "a"),
        ("https://ex.invalid/b", folders["Technology"], "b"),
        ("https://ex.invalid/c", folders["Food"], "c"),
    ]
    assert result.added == 3 and not result.failed


@pytest.mark.asyncio
async def test_starter_reuses_an_existing_folder_of_that_name(pg, fake_subscribe):
    calls, _ = fake_subscribe
    user = await _user(pg)
    pg.add(Folder(user_id=user.id, name="Food", position=1))
    await pg.flush()
    await starter.subscribe_starter(user, ["food"], pg)
    folders = await _folders(pg, user)
    assert list(folders) == ["Food"]
    assert calls[0][1] == folders["Food"]


@pytest.mark.asyncio
async def test_a_failing_feed_is_left_out_and_the_rest_go_in(pg, fake_subscribe):
    calls, outcomes = fake_subscribe
    outcomes["https://ex.invalid/a"] = ValueError("unreachable")
    outcomes["https://ex.invalid/b"] = AlreadySubscribed()
    user = await _user(pg)
    result = await starter.subscribe_starter(user, ["tech", "food"], pg)
    assert [c[0] for c in calls] == ["https://ex.invalid/c"]
    assert (result.added, result.skipped, result.failed) == (1, 1, ["a"])


@pytest.mark.asyncio
async def test_feed_limit_stops_the_rest(pg, fake_subscribe):
    calls, outcomes = fake_subscribe
    outcomes["https://ex.invalid/b"] = FeedLimitReached(1)
    user = await _user(pg)
    result = await starter.subscribe_starter(user, ["tech", "food"], pg)
    assert [c[0] for c in calls] == ["https://ex.invalid/a"]
    assert result.over_limit and result.added == 1


# ── the flow ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_step_one_leads_to_step_two_without_finishing(pg):
    from app.routers.web.app.welcome import welcome_save

    user = await _user(pg)
    resp = await welcome_save(request=None, relevance_terms="rust", skip="", user=user, db=pg)
    assert resp.headers["HX-Redirect"] == "/welcome/feeds"
    resp = await welcome_save(request=None, relevance_terms="", skip="1", user=user, db=pg)
    assert resp.headers["HX-Redirect"] == "/welcome/feeds"
    assert await _onboarded_at(pg, user) is None


@pytest.mark.asyncio
async def test_app_resumes_the_welcome_where_it_was_left(pg):
    from app.routers.web.app.shell import main_app
    from app.routers.web.app.welcome import welcome_save

    user = await _user(pg)
    pg.add(UserSettings(user_id=user.id))
    await pg.flush()
    resp = await main_app(request=None, user=user, db=pg)
    assert resp.status_code == 303 and resp.headers["location"] == "/welcome"

    await welcome_save(request=None, relevance_terms="rust", skip="", user=user, db=pg)
    resp = await main_app(request=None, user=user, db=pg)
    assert resp.status_code == 303 and resp.headers["location"] == "/welcome/feeds"


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,target", [
    ("import", "/settings/opml"), ("manual", "/settings/feeds"),
])
async def test_bring_your_own_finishes_and_leads_to_settings(pg, choice, target):
    from app.routers.web.app.welcome import welcome_feeds_save

    user = await _user(pg)
    resp = await welcome_feeds_save(request=None, choice=choice, category=[], user=user, db=pg)
    assert resp.headers["HX-Redirect"] == target
    assert await _onboarded_at(pg, user) is not None


@pytest.mark.asyncio
async def test_step_two_does_nothing_once_the_welcome_is_over(pg, fake_subscribe):
    from app.routers.web.app.welcome import welcome_feeds_save

    calls, _ = fake_subscribe
    user = await _user(pg)
    await welcome_feeds_save(request=None, choice="manual", category=[], user=user, db=pg)
    resp = await welcome_feeds_save(request=None, choice="starter", category=["tech"], user=user, db=pg)
    assert resp.headers["HX-Redirect"] == "/app"
    assert calls == [] and await _folders(pg, user) == {}


@pytest.mark.asyncio
async def test_starter_choice_finishes_only_when_a_feed_went_in(pg, fake_subscribe):
    from app.routers.web.app.welcome import welcome_feeds_save

    _, outcomes = fake_subscribe
    user = await _user(pg)

    resp = await welcome_feeds_save(request=None, choice="starter", category=[], user=user, db=pg)
    assert "HX-Redirect" not in resp.headers

    outcomes["https://ex.invalid/c"] = ValueError("unreachable")
    resp = await welcome_feeds_save(request=None, choice="starter", category=["food"], user=user, db=pg)
    assert "HX-Redirect" not in resp.headers
    assert await _onboarded_at(pg, user) is None

    resp = await welcome_feeds_save(request=None, choice="starter", category=["tech"], user=user, db=pg)
    assert resp.headers["HX-Redirect"] == "/app"
    assert await _onboarded_at(pg, user) is not None

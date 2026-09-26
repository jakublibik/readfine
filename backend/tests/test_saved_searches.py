"""Saved searches: parameter validation shared with the ad-hoc search, the service's
rules (name, limit, ownership) and cleanup when a feed, folder or label goes away.

Pure tests need no DB; the rest run against the real (dev) DB in a rolled-back
transaction and skip if unreachable.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import Label
from app.models.saved_search import SavedSearch
from app.models.user import User
from app.services import saved_search_service as svc
from app.services.article import list_articles
from app.services.label_service import delete_label
from app.services.saved_search_service import SavedSearchError
from app.services.scope_cleanup import strip_scope_references
from app.services.search_params import list_kwargs, normalize_search_params

# ── normalize_search_params (pure) ───────────────────────────────────────────

class TestNormalize:
    def test_query_string_form(self):
        raw = {
            "q": "  rust  ", "sort": "newest", "read_status": "unread",
            "scope_include": '["feed:5", "folder:0", "feed:5"]',
            "label_filter": '["label:3"]',
            "score_source": "basic", "score_op": "gte", "score_val": "60",
            "since_days": "7", "state": "starred",
        }
        assert normalize_search_params(raw) == {
            "q": "rust", "sort": "newest", "read_status": "unread",
            "scope_include": ["feed:5", "folder:0"], "label_filter": ["label:3"],
            "score_source": "basic", "score_op": "gte", "score_val": 60.0,
            "since_days": 7, "state": "starred",
        }

    def test_stored_form_is_a_fixed_point(self):
        once = normalize_search_params({
            "q": "x", "scope_include": ["feed:1"], "label_filter": ["any"],
            "sort": "score", "score_source": "ai",
        })
        assert normalize_search_params(once) == once

    def test_defaults_and_junk_are_dropped(self):
        assert normalize_search_params({
            "q": "   ", "sort": "relevance", "read_status": "all",
            "scope_include": "not json", "label_filter": '["label:x", 5]',
            "since_days": "0", "state": "bogus", "offset": "40", "evil": "1",
        }) == {}

    def test_bad_tokens_skipped(self):
        assert normalize_search_params(
            {"scope_include": ["feed:1", "label:2", "feed:-3", "folder:abc", 7]}
        ) == {"scope_include": ["feed:1"]}

    def test_any_label_wins(self):
        assert normalize_search_params({"label_filter": ["label:1", "any"]}) == {
            "label_filter": ["any"]}

    def test_score_condition_needs_operator_and_value_in_range(self):
        assert normalize_search_params({"score_op": "gte"}) == {}
        assert normalize_search_params({"score_op": "gte", "score_val": "150"}) == {}
        # Out-of-range condition with a score sort keeps just the source.
        assert normalize_search_params(
            {"sort": "score", "score_source": "bogus", "score_op": "lt", "score_val": 101}
        ) == {"sort": "score", "score_source": "relevance"}

    def test_since_days_bounds(self):
        assert normalize_search_params({"since_days": 3650}) == {"since_days": 3650}
        assert normalize_search_params({"since_days": 3651}) == {}
        assert normalize_search_params({"since_days": True}) == {}

    def test_list_kwargs_back_to_query_string_shapes(self):
        kw = list_kwargs(normalize_search_params({
            "q": "x", "scope_include": ["feed:1"], "label_filter": ["label:2"],
            "score_op": "gte", "score_val": 50, "score_source": "ai",
        }))
        assert kw["sort"] == "relevance"
        assert json.loads(kw["scope_include"]) == ["feed:1"]
        assert json.loads(kw["label_filter"]) == ["label:2"]
        assert kw["score"] == {"score_source": "ai", "score_op": "gte", "score_val": 50.0}
        assert kw["read_status"] is None and kw["state"] is None


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


async def _save(session, user, name="s", **params):
    return await svc.create_saved_search(
        session, user.id, name=name, params=params or {"q": "x"},
    )


async def _feed(session, user=None):
    u = uuid.uuid4().hex
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title=f"feed-{u[:6]}", subscriber_count=1)
    session.add(feed)
    await session.flush()
    if user is not None:
        session.add(UserFeed(user_id=user.id, feed_id=feed.id))
        await session.flush()
    return feed


# ── service rules ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_stores_normalized_params(pg):
    user = await _user(pg)
    s = await _save(pg, user, "  Rust   news ", q=" rust ", scope_include='["feed:4"]', sort="relevance")
    assert s.name == "Rust news"
    assert s.params == {"q": "rust", "scope_include": ["feed:4"]}


@pytest.mark.asyncio
async def test_order_only_or_empty_params_rejected(pg):
    user = await _user(pg)
    with pytest.raises(SavedSearchError):
        await _save(pg, user, sort="score", score_source="ai")
    with pytest.raises(SavedSearchError):
        await svc.create_saved_search(pg, user.id, name="e", params={"q": " "})


@pytest.mark.asyncio
async def test_name_required_and_unique_case_insensitive(pg):
    user = await _user(pg)
    with pytest.raises(SavedSearchError):
        await _save(pg, user, "   ")
    await _save(pg, user, "Tech")
    with pytest.raises(SavedSearchError):
        await _save(pg, user, "tech")
    # Another user may use the same name.
    other = await _user(pg)
    await _save(pg, other, "Tech")


@pytest.mark.asyncio
async def test_update_rename_rules(pg):
    user = await _user(pg)
    a = await _save(pg, user, "Alpha")
    await _save(pg, user, "Beta")
    # Changing only the case of its own name is fine.
    await svc.update_saved_search(pg, a, name="ALPHA")
    assert a.name == "ALPHA"
    with pytest.raises(SavedSearchError):
        await svc.update_saved_search(pg, a, name="beta")


@pytest.mark.asyncio
async def test_update_keeps_what_is_not_passed(pg):
    user = await _user(pg)
    s = await _save(pg, user, "S", q="old", since_days=7)
    await svc.update_saved_search(pg, s, params={"q": "new"})
    assert s.params == {"q": "new"} and s.name == "S"
    await svc.update_saved_search(pg, s, name="T")
    assert s.params == {"q": "new"} and s.name == "T"


@pytest.mark.asyncio
async def test_limit_per_user(pg, monkeypatch):
    monkeypatch.setattr(svc, "MAX_SAVED_SEARCHES", 2)
    user = await _user(pg)
    await _save(pg, user, "a")
    await _save(pg, user, "b")
    with pytest.raises(SavedSearchError):
        await _save(pg, user, "c")


@pytest.mark.asyncio
async def test_other_users_search_is_not_found(pg):
    owner = await _user(pg)
    intruder = await _user(pg)
    s = await _save(pg, owner)
    assert await svc.get_saved_search(pg, owner.id, s.id) is s
    assert await svc.get_saved_search(pg, intruder.id, s.id) is None


@pytest.mark.asyncio
async def test_list_alphabetical(pg):
    user = await _user(pg)
    await _save(pg, user, "beta")
    await _save(pg, user, "Alpha")
    await _save(pg, user, "gamma")
    assert [s.name for s in await svc.list_saved_searches(pg, user.id)] == ["Alpha", "beta", "gamma"]


# ── cleanup of deleted feeds, folders and labels ─────────────────────────────

@pytest.mark.asyncio
async def test_feed_strip_narrows_scope(pg):
    user = await _user(pg)
    s = await _save(pg, user, "two", scope_include=["feed:5", "feed:50"])
    res = await strip_scope_references(pg, kind="feed", ref_id=5, user_id=user.id)
    await pg.flush()
    await pg.refresh(s)
    assert s.params["scope_include"] == ["feed:50"]
    assert res.emptied_searches == []


@pytest.mark.asyncio
async def test_last_reference_stays_and_is_reported(pg):
    # Removing it would widen the search to every feed; deleting the search would
    # lose its query. It stays, matches nothing, and the reader is told.
    user = await _user(pg)
    s = await _save(pg, user, "only-9", q="x", scope_include=["folder:9"])
    res = await strip_scope_references(pg, kind="folder", ref_id=9, user_id=user.id)
    await pg.flush()
    await pg.refresh(s)
    assert s.params == {"q": "x", "scope_include": ["folder:9"]}
    assert res.emptied_searches == ["only-9"]
    assert res.has_changes


@pytest.mark.asyncio
async def test_unsubscribed_feed_in_scope_matches_nothing(pg):
    # What the reference left in place does: an unsubscribed feed's articles are not
    # the reader's, so the search comes up empty rather than widening.
    user = await _user(pg)
    feed = await _feed(pg)  # someone else's subscription, not this user's
    pg.add(Article(feed_id=feed.id, guid=uuid.uuid4().hex, guid_hash=uuid.uuid4().hex,
                   title="x", content="<p>x</p>", readable_status="success",
                   published_at=datetime.now(timezone.utc), fetched_at=datetime.now(timezone.utc)))
    await pg.flush()
    kw = list_kwargs({"scope_include": [f"feed:{feed.id}"]})
    rows = await list_articles(
        user=user, db=pg, scope_include=kw["scope_include"], search=True, limit=10,
    )
    assert rows == []


@pytest.mark.asyncio
async def test_missing_references(pg):
    user = await _user(pg)
    mine = await _feed(pg, user)
    label = Label(user_id=user.id, name="l")
    pg.add(label)
    await pg.flush()
    ok = {"scope_include": [f"feed:{mine.id}", "folder:0"], "label_filter": [f"label:{label.id}"]}
    assert await svc.has_missing_references(pg, user.id, ok) is False
    assert await svc.has_missing_references(pg, user.id, {"scope_include": ["feed:999999999"]})
    assert await svc.has_missing_references(pg, user.id, {"scope_include": ["folder:999999999"]})
    assert await svc.has_missing_references(pg, user.id, {"label_filter": ["label:999999999"]})
    assert await svc.has_missing_references(pg, user.id, {"label_filter": ["any"]}) is False


@pytest.mark.asyncio
async def test_strip_is_scoped_to_the_user(pg):
    a = await _user(pg)
    b = await _user(pg)
    sa = await _save(pg, a, scope_include=["feed:5", "feed:6"])
    sb = await _save(pg, b, scope_include=["feed:5", "feed:6"])
    await strip_scope_references(pg, kind="feed", ref_id=5, user_id=a.id)
    await pg.flush()
    await pg.refresh(sa)
    await pg.refresh(sb)
    assert sa.params["scope_include"] == ["feed:6"]
    assert sb.params["scope_include"] == ["feed:5", "feed:6"]


@pytest.mark.asyncio
async def test_label_delete_strips_label_filter(pg):
    user = await _user(pg)
    keep = Label(user_id=user.id, name="keep")
    gone = Label(user_id=user.id, name="gone")
    pg.add_all([keep, gone])
    await pg.flush()
    gone_id = gone.id
    both = await _save(pg, user, "both", label_filter=[f"label:{keep.id}", f"label:{gone_id}"])
    only = await _save(pg, user, "only", label_filter=[f"label:{gone_id}"])
    await delete_label(user, gone_id, pg)
    await pg.refresh(both)
    await pg.refresh(only)
    assert both.params["label_filter"] == [f"label:{keep.id}"]
    assert only.params["label_filter"] == [f"label:{gone_id}"]



@pytest.mark.asyncio
async def test_account_delete_cascades(pg):
    user = await _user(pg)
    s = await _save(pg, user)
    sid = s.id
    await pg.delete(user)
    await pg.flush()
    pg.expunge_all()
    assert await pg.get(SavedSearch, sid) is None


# ── web routes: another user's saved search ──────────────────────────────────

@pytest.mark.asyncio
async def test_routes_refuse_another_users_search(pg):
    from fastapi import HTTPException

    from app.routers.web.app.articles import htmx_article_list
    from app.routers.web.app.saved_searches import (
        htmx_delete_saved_search,
        htmx_update_saved_search,
    )
    from app.routers.web.app.shell import htmx_search_modal

    owner = await _user(pg)
    intruder = await _user(pg)
    s = await _save(pg, owner, "mine")

    with pytest.raises(HTTPException) as exc:
        await htmx_article_list(request=None, saved_search_id=s.id, user=intruder, db=pg)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await htmx_search_modal(request=None, saved_id=s.id, edited=False, user=intruder, db=pg)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await htmx_update_saved_search(search_id=s.id, request=None, user=intruder, db=pg)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await htmx_delete_saved_search(search_id=s.id, user=intruder, db=pg)
    assert exc.value.status_code == 404
    assert await svc.get_saved_search(pg, owner.id, s.id) is s


# ── a saved search opened as a view ──────────────────────────────────────────

async def _art(session, feed, title, *, hours_ago=1, read=None, user=None):
    when = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    g = uuid.uuid4().hex
    a = Article(feed_id=feed.id, guid=g, guid_hash=g, title=title, content="<p>x</p>",
                readable_status="success", published_at=when, fetched_at=when)
    session.add(a)
    await session.flush()
    if read is not None:
        session.add(UserArticleState(user_id=user.id, article_id=a.id, is_read=read))
        await session.flush()
    return a


async def _is_read(session, user, article):
    st = await session.get(UserArticleState, (user.id, article.id))
    if st is not None:
        await session.refresh(st)
    return bool(st and st.is_read)


@pytest.mark.asyncio
async def test_view_status_follows_the_unread_setting_only_without_its_own(pg):
    from app.routers.web.app.articles import saved_view_unread_only

    user = await _user(pg)
    feed = await _feed(pg, user)
    token = "zview" + uuid.uuid4().hex[:10]
    unread = await _art(pg, feed, f"{token} one")
    await _art(pg, feed, f"{token} two", read=True, user=user)
    filters = {"q": token}

    async def check(setting, **kw):
        return await saved_view_unread_only(
            user, pg, setting, read_status=kw.get("read_status"),
            show_read=kw.get("show_read", False), filters=filters,
        )

    assert await check("adaptive") is True
    assert await check("unread_only") is True
    assert await check("show_all") is False
    # Its own status does the filtering; "Show read too" drops the setting.
    assert await check("adaptive", read_status="not_engaged") is False
    assert await check("unread_only", show_read=True) is False
    # Adaptive with nothing unread left shows everything.
    pg.add(UserArticleState(user_id=user.id, article_id=unread.id, is_read=True))
    await pg.flush()
    assert await check("adaptive") is False
    assert await check("unread_only") is True


@pytest.mark.asyncio
async def test_mark_read_marks_exactly_what_the_view_lists(pg):
    user = await _user(pg)
    other = await _user(pg)
    feed_a = await _feed(pg, user)
    feed_b = await _feed(pg, user)
    pg.add(UserFeed(user_id=other.id, feed_id=feed_a.id))
    await pg.flush()
    token = "zmark" + uuid.uuid4().hex[:10]
    match = await _art(pg, feed_a, f"{token} match")
    no_term = await _art(pg, feed_a, "something else")
    out_of_scope = await _art(pg, feed_b, f"{token} other feed")
    too_new = await _art(pg, feed_a, f"{token} arrived later", hours_ago=0)
    s = await _save(pg, user, "view", q=token, scope_include=[f"feed:{feed_a.id}"])

    before = datetime.now(timezone.utc) - timedelta(minutes=30)
    await svc.mark_saved_search_read(pg, user, s, before=before)
    await pg.flush()

    assert await _is_read(pg, user, match)
    assert not await _is_read(pg, user, no_term)
    assert not await _is_read(pg, user, out_of_scope)
    assert not await _is_read(pg, user, too_new)
    # Only this reader's state changes.
    assert not await _is_read(pg, other, match)

"""OPML export → import of a Readfine account: the sections beyond feeds (filters with
their order, labels, preferences, relevance terms and AI texts, saved searches,
catch-ups) and the per-feed settings.

Pure tests need no DB; the round trip runs against the real (dev) DB in a
rolled-back transaction and skips if unreachable.
"""
import json
import uuid
from types import SimpleNamespace
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import selectinload

from app.config import settings as app_settings
from app.models.feed import Feed, Folder, UserFeed
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import Label
from app.models.saved_search import SavedSearch
from app.models.user import User, UserCatchupConfig, UserSettings
from app.services.opml import (
    READFINE_NS,
    ImportResult,
    _ExportRefs,
    _apply_briefing_schedule,
    _apply_prefs,
    _collect_feed_outlines,
    _export_filter,
    _feed_options,
    _feed_outline,
    _parse_readfine_filter,
    export_opml,
    import_opml,
)


def _filter(**kw):
    base = dict(
        name="F", is_active=True, position=0, match_operator="AND", stop_on_match=False,
        scope_include=None, scope_except=None, conditions=[], actions=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


# ── Filters (pure) ────────────────────────────────────────────────────────────

class TestFilterExport:
    def test_label_action_by_name_and_position_kept(self):
        f = _filter(
            position=3,
            actions=[SimpleNamespace(action_type="label", action_value="7")],
        )
        out = _export_filter(f, _ExportRefs({}, {}, {7: "2024"}))
        assert out["position"] == 3
        assert out["actions"] == [{"action_type": "label", "action_value": "2024"}]

    def test_action_on_deleted_label_left_out(self):
        f = _filter(actions=[
            SimpleNamespace(action_type="label", action_value="99"),
            SimpleNamespace(action_type="star", action_value=None),
        ])
        out = _export_filter(f, _ExportRefs({}, {}, {}))
        assert out["actions"] == [{"action_type": "star", "action_value": None}]


class TestFilterImport:
    _cond = [{"field": "title", "operator": "contains", "value": "x"}]

    def test_numeric_label_name_is_a_name_not_an_id(self):
        fd = {"match_operator": "AND", "conditions": self._cond,
              "actions": [{"action_type": "label", "action_value": "2024"}]}
        payload = _parse_readfine_filter(fd, {"2024": 12}, {}, {}, ImportResult())
        assert payload.actions[0].action_value == "12"

    def test_position_restored(self):
        fd = {"match_operator": "AND", "conditions": self._cond, "position": 4}
        assert _parse_readfine_filter(fd, {}, {}, {}, ImportResult()).position == 4

    def test_scope_with_nothing_found_imports_switched_off(self):
        """An empty scope means every feed, so a filter meant for one feed must not
        come back active on all of them."""
        res = ImportResult()
        fd = {"match_operator": "AND", "conditions": self._cond, "enabled": True,
              "scope_include": ["feed:https://gone.invalid/rss"]}
        payload = _parse_readfine_filter(fd, {}, {}, {}, res)
        assert payload.is_active is False
        assert payload.scope_include == []
        assert any("switched off" in w for w in res.warnings)

    def test_partly_found_scope_stays_active(self):
        fd = {"match_operator": "AND", "conditions": self._cond,
              "scope_include": ["feed:https://a.invalid/rss", "feed:https://gone.invalid/rss"]}
        payload = _parse_readfine_filter(fd, {}, {"https://a.invalid/rss": 5}, {}, ImportResult())
        assert payload.is_active is True
        assert payload.scope_include == ["feed:5"]


# ── Feeds and sections (pure) ─────────────────────────────────────────────────

class TestFeedOptions:
    def test_outline_carries_subscription_settings(self):
        parent = Element("body")
        uf = SimpleNamespace(custom_title=None, extract_readable=False, ai_summary_enabled=True,
                             purge_after_days=30, purge_keep_count=None)
        feed = SimpleNamespace(title="T", feed_url="https://a.invalid/rss", site_url=None,
                               feed_type="rss", type_config=None)
        _feed_outline(parent, uf, feed)
        el = parent[0]
        assert el.get("extract-readable") == "0"
        assert el.get("ai-summary") == "1"
        assert el.get("purge-after-days") == "30"
        assert el.get("purge-keep-count") is None
        assert _feed_options(el) == {
            "extract_readable": False, "ai_summary_enabled": True, "purge_after_days": 30,
        }

    def test_out_of_range_retention_ignored(self):
        el = Element("outline", {"purge-after-days": "0", "purge-keep-count": "99999"})
        assert _feed_options(el) == {}


class TestPrefs:
    def test_valid_values_set_invalid_warned(self):
        us = SimpleNamespace(
            list_density_web="comfortable", unread_filter="adaptive", mark_read_on_scroll=True,
            articles_per_page=50, bucket_small_max=640, bucket_medium_max=1100,
            folders_arranged=False,
        )
        res = ImportResult()
        _apply_prefs(us, {
            "list_density_web": "compact", "unread_filter": "everything",
            "mark_read_on_scroll": False, "articles_per_page": 5000,
            "bucket_small_max": 900, "bucket_medium_max": 700,
        }, res)
        assert us.list_density_web == "compact"
        assert us.unread_filter == "adaptive"
        assert us.mark_read_on_scroll is False
        assert us.articles_per_page == 200
        assert (us.bucket_small_max, us.bucket_medium_max) == (900, 1000)
        assert any("unread_filter" in w for w in res.warnings)


class TestBriefingSchedule:
    def _cfg(self):
        return SimpleNamespace(briefing_interval=None, briefing_day=None,
                               briefing_time=None, briefing_recipients=None)

    def test_valid_weekly(self):
        cfg = self._cfg()
        ok = _apply_briefing_schedule(cfg, {"interval": "weekly", "day": 2, "time": "07:30",
                                            "recipients": ["a@example.com"]})
        assert ok
        assert (cfg.briefing_interval, cfg.briefing_day, cfg.briefing_time) == ("weekly", 2, "07:30")
        assert json.loads(cfg.briefing_recipients) == ["a@example.com"]

    @pytest.mark.parametrize("briefing", [
        {"interval": "hourly", "time": "08:00"},
        {"interval": "weekly", "day": 9, "time": "08:00"},
        {"interval": "daily", "time": "25:00"},
        {"interval": "daily", "time": "08:00", "recipients": ["not an email"]},
    ])
    def test_invalid_rejected(self, briefing):
        assert not _apply_briefing_schedule(self._cfg(), briefing)


# ── Round trip (DB) ───────────────────────────────────────────────────────────

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


async def _user(session, **settings):
    u = uuid.uuid4().hex
    user = User(email=f"{u}@ex.invalid", password_hash="x", display_name=f"u-{u[:6]}")
    session.add(user)
    await session.flush()
    session.add(UserSettings(user_id=user.id, **settings))
    await session.flush()
    return user


async def _import_all(session, user, xml: str) -> ImportResult:
    return await import_opml(
        user=user, xml_bytes=xml.encode(), import_feeds=False, import_labels=True,
        import_prefs=True, import_filters=True, import_profile=True,
        import_searches=True, import_catchup=True, db=session,
    )


@pytest.mark.asyncio
async def test_account_round_trip(pg):
    feed = Feed(feed_url=f"https://ex.invalid/{uuid.uuid4().hex}.xml", title="Feed", subscriber_count=2)
    pg.add(feed)
    await pg.flush()

    # Source account
    a = await _user(pg, unread_filter="unread_only", reading_font_family="serif",
                    relevance_terms="rust\npostgres", ai_summary_prompt="Be brief.")
    folder_a = Folder(user_id=a.id, name="Tech")
    label_a = Label(user_id=a.id, name="2024", color="#ff0000")
    pg.add_all([folder_a, label_a])
    await pg.flush()
    pg.add(UserFeed(user_id=a.id, feed_id=feed.id, folder_id=folder_a.id))
    for pos, name in ((2, "A last"), (1, "B first")):
        pg.add(Filter(
            user_id=a.id, name=name, position=pos, stop_on_match=True,
            scope_include=json.dumps([f"feed:{feed.id}"]),
            conditions=[FilterCondition(field="title", operator="contains", value=name)],
            actions=[FilterAction(action_type="label", action_value=str(label_a.id))],
        ))
    pg.add(SavedSearch(user_id=a.id, name="Rust", params={
        "q": "rust", "scope_include": [f"folder:{folder_a.id}"], "label_filter": [f"label:{label_a.id}"],
    }))
    pg.add(UserCatchupConfig(
        user_id=a.id, name="Morning", period="today", scope_include=json.dumps([f"feed:{feed.id}"]),
        filter_score_min=0.6, article_limit=100, briefing_enabled=True,
        briefing_interval="daily", briefing_time="07:00",
    ))
    await pg.flush()

    xml = await export_opml(a, pg)

    # Target account: the same feed in a folder of the same name, nothing else.
    b = await _user(pg)
    folder_b = Folder(user_id=b.id, name="Tech")
    pg.add(folder_b)
    await pg.flush()
    pg.add(UserFeed(user_id=b.id, feed_id=feed.id, folder_id=folder_b.id))
    await pg.flush()

    result = await _import_all(pg, b, xml)
    assert result.labels_added == 1

    label_b = await pg.scalar(select(Label).where(Label.user_id == b.id))
    assert (label_b.name, label_b.color) == ("2024", "#ff0000")

    filters = (await pg.execute(
        select(Filter).where(Filter.user_id == b.id)
        .options(selectinload(Filter.actions)).order_by(Filter.position)
    )).scalars().all()
    assert [(f.name, f.position, f.stop_on_match) for f in filters] == [
        ("B first", 1, True), ("A last", 2, True),
    ]
    assert json.loads(filters[0].scope_include) == [f"feed:{feed.id}"]
    assert filters[0].actions[0].action_value == str(label_b.id)

    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == b.id))
    await pg.refresh(s)
    assert s.unread_filter == "unread_only"
    assert s.reading_font_family == "serif"
    assert s.relevance_terms == "rust\npostgres"
    assert s.ai_summary_prompt == "Be brief."

    search = await pg.scalar(select(SavedSearch).where(SavedSearch.user_id == b.id))
    assert search.params == {
        "q": "rust", "scope_include": [f"folder:{folder_b.id}"], "label_filter": [f"label:{label_b.id}"],
    }

    cfg = await pg.scalar(select(UserCatchupConfig).where(UserCatchupConfig.user_id == b.id))
    assert (cfg.name, cfg.period, cfg.article_limit) == ("Morning", "today", 100)
    assert cfg.filter_score_min == pytest.approx(0.6)
    assert json.loads(cfg.scope_include) == [f"feed:{feed.id}"]
    # The schedule comes over, the sending does not.
    assert (cfg.briefing_interval, cfg.briefing_time, cfg.briefing_enabled) == ("daily", "07:00", False)
    assert any("switched off" in w for w in result.warnings)

    # A second run finds everything already there.
    again = await _import_all(pg, b, xml)
    assert (again.labels_added, again.filters_added, again.searches_added, again.catchups_added) == (0, 0, 0, 0)
    assert (again.filters_skipped, again.searches_skipped, again.catchups_skipped) == (2, 1, 1)


@pytest.mark.asyncio
async def test_filter_label_missing_from_file_is_created(pg):
    """Filters imported without the labels section still keep their label action."""
    user = await _user(pg)
    xml = (
        '<opml version="2.0"><body><outline text="tt-rss-filters">'
        + json.dumps([{
            "name": "L", "match_operator": "AND",
            "conditions": [{"field": "title", "operator": "contains", "value": "x"}],
            "actions": [{"action_type": "label", "action_value": "Later"}],
        }])
        + "</outline></body></opml>"
    )
    result = await import_opml(
        user=user, xml_bytes=xml.encode(), import_feeds=False, import_labels=False,
        import_prefs=False, import_filters=True, db=pg,
    )
    label = await pg.scalar(select(Label).where(Label.user_id == user.id))
    assert label.name == "Later"
    assert result.filters_added == 1 and result.labels_added == 1


@pytest.mark.asyncio
async def test_feeds_only_export_is_plain_opml(pg):
    user = await _user(pg, relevance_terms="x")
    pg.add(Label(user_id=user.id, name="L"))
    await pg.flush()
    root = ET.fromstring(await export_opml(user, pg, sections=["feeds"]))
    assert len(root.find("head")) == 2  # title and dateCreated, nothing of ours
    assert [o.get("text") for o in root.find("body")] == []


@pytest.mark.asyncio
async def test_full_export_keeps_body_to_feeds_and_tt_rss_sections(pg):
    """What only Readfine reads goes in <head>: an outline in <body> that another
    reader does not know becomes an empty folder there."""
    user = await _user(pg, relevance_terms="x")
    pg.add(SavedSearch(user_id=user.id, name="S", params={"q": "x"}))
    pg.add(UserCatchupConfig(user_id=user.id, name="C"))
    await pg.flush()
    root = ET.fromstring(await export_opml(user, pg))
    assert {o.get("text") for o in root.find("body")} <= {"tt-rss-labels", "tt-rss-prefs", "tt-rss-filters"}
    names = {el.get("name") for el in root.findall(f"head/{{{READFINE_NS}}}section")}
    assert names == {"prefs", "profile", "saved-searches", "catchup"}
    assert root.find(f"head/{{{READFINE_NS}}}format").text == "2"


def _legacy_filter_xml(label_value: str, versioned: bool) -> bytes:
    head = f'<head><readfine:format xmlns:readfine="{READFINE_NS}">2</readfine:format></head>' if versioned else "<head/>"
    return (
        f'<opml version="2.0">{head}<body><outline text="tt-rss-filters">'
        + json.dumps([{
            "name": "L", "match_operator": "AND",
            "conditions": [{"field": "title", "operator": "contains", "value": "x"}],
            "actions": [{"action_type": "label", "action_value": label_value}, {"action_type": "star"}],
        }])
        + "</outline></body></opml>"
    ).encode()


@pytest.mark.asyncio
async def test_old_export_digits_are_not_made_into_a_label(pg):
    """Before names were used everywhere, an export wrote the id of a deleted label."""
    user = await _user(pg)
    result = await import_opml(
        user=user, xml_bytes=_legacy_filter_xml("17", versioned=False), import_feeds=False,
        import_labels=False, import_prefs=False, import_filters=True, db=pg,
    )
    assert await pg.scalar(select(Label).where(Label.user_id == user.id)) is None
    assert result.filters_added == 1  # kept, with its other action


@pytest.mark.asyncio
async def test_current_export_digit_label_is_created(pg):
    user = await _user(pg)
    await import_opml(
        user=user, xml_bytes=_legacy_filter_xml("2024", versioned=True), import_feeds=False,
        import_labels=False, import_prefs=False, import_filters=True, db=pg,
    )
    label = await pg.scalar(select(Label).where(Label.user_id == user.id))
    assert label.name == "2024"


_JUNK = [None, [], {}, "x", True, 10**30, float("inf"), float("nan"), -1]


@pytest.mark.asyncio
@pytest.mark.parametrize("junk", _JUNK, ids=repr)
async def test_damaged_values_never_break_the_import(pg, junk):
    """Every value in every section swapped for junk: the import may skip things
    and warn, but must not fail (the route only turns ValueError into a message)."""
    user = await _user(pg)
    prefs = {k: junk for k in (
        "list_density_web", "unread_filter", "story_dedup", "folder_order", "format_profile",
        "mark_read_on_scroll", "articles_per_page", "bucket_small_max", "bucket_medium_max",
    )}
    profile = {k: junk for k in (
        "basic_scoring_enabled", "relevance_terms", "ai_profile", "ai_summary_prompt",
    )}
    search = {"name": junk, "params": {k: junk for k in (
        "q", "sort", "read_status", "scope_include", "label_filter",
        "score_source", "score_op", "score_val", "since_days", "state",
    )}}
    catchup = {k: junk for k in (
        "name", "period", "filter_status", "scope_include", "label_filter", "score_min",
        "article_limit", "custom_prompt", "include_snippet",
    )}
    catchup["name"] = "C"
    catchup["briefing"] = {k: junk for k in ("enabled", "interval", "day", "time", "recipients")}
    filt = {"name": junk, "match_operator": "AND", "position": junk, "enabled": junk,
            "scope_include": junk, "conditions": junk, "actions": junk}

    def sec(name, payload):
        return f'<readfine:section name="{name}">{json.dumps(payload)}</readfine:section>'

    xml = (
        f'<opml version="2.0" xmlns:readfine="{READFINE_NS}"><head>'
        '<readfine:format>2</readfine:format>'
        + sec("prefs", prefs) + sec("profile", profile)
        + sec("saved-searches", [search, junk]) + sec("catchup", [catchup, junk])
        + '</head><body><outline text="tt-rss-filters">' + json.dumps([filt, junk])
        + "</outline></body></opml>"
    )
    await import_opml(
        user=user, xml_bytes=xml.encode(), import_feeds=False, import_labels=True,
        import_prefs=True, import_filters=True, import_profile=True,
        import_searches=True, import_catchup=True, db=pg,
    )

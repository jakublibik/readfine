"""Unit tests for OPML import parsing/mapping (pure functions, no DB) plus
import_opml input validation. The DB-driven import paths are covered indirectly;
here we lock down the TTRSS↔Readfine mapping and structural parsing.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET
import pytest

from app.services.opml import (
    ImportResult,
    _collect_feed_outlines,
    _feed_outline,
    _find_section,
    _dedupe_conditions,
    _import_feed,
    _looks_like_regex,
    _parse_readfine_filter,
    _parse_ttrss_filter,
    _resolve_scope,
    _scope_to_urls,
    import_opml,
)
from app.schemas.filter import FilterConditionCreate


def _body(xml: str):
    return ET.fromstring(xml).find("body") if "<body" in xml else ET.fromstring(xml)


# ── _looks_like_regex ─────────────────────────────────────────────────────────

class TestLooksLikeRegex:
    def test_plain_text_is_not_regex(self):
        assert _looks_like_regex("breaking news") is False

    @pytest.mark.parametrize("val", ["foo.*bar", "^start", "a|b", "(group)", "x+"])
    def test_regex_specials_detected(self, val):
        assert _looks_like_regex(val) is True


# ── _collect_feed_outlines ────────────────────────────────────────────────────

class TestCollectFeedOutlines:
    def test_flat_feeds(self):
        body = ET.fromstring(
            '<body>'
            '<outline xmlUrl="http://a.com/rss"/>'
            '<outline xmlUrl="http://b.com/rss"/>'
            '</body>'
        )
        out = _collect_feed_outlines(body)
        assert {o.get("xmlUrl") for o, _ in out} == {"http://a.com/rss", "http://b.com/rss"}
        assert all(folder is None for _, folder in out)

    def test_feeds_in_folder(self):
        body = ET.fromstring(
            '<body>'
            '<outline text="Tech"><outline xmlUrl="http://a.com/rss"/></outline>'
            '</body>'
        )
        out = _collect_feed_outlines(body)
        assert len(out) == 1
        outline, folder = out[0]
        assert outline.get("xmlUrl") == "http://a.com/rss"
        assert folder == "Tech"

    def test_ttrss_all_articles_wrapper_unwrapped(self):
        body = ET.fromstring(
            '<body>'
            '<outline text="All articles">'
            '<outline text="News"><outline xmlUrl="http://a.com/rss"/></outline>'
            '</outline>'
            '</body>'
        )
        out = _collect_feed_outlines(body)
        assert len(out) == 1
        outline, folder = out[0]
        assert outline.get("xmlUrl") == "http://a.com/rss"
        assert folder == "News"

    def test_section_outlines_ignored(self):
        body = ET.fromstring(
            '<body>'
            '<outline text="tt-rss-labels"><outline text="-x"/></outline>'
            '<outline xmlUrl="http://a.com/rss"/>'
            '</body>'
        )
        out = _collect_feed_outlines(body)
        assert len(out) == 1
        assert out[0][0].get("xmlUrl") == "http://a.com/rss"

    def test_feedly_layout(self):
        # Mirrors real Feedly exports (verified against live samples): flat
        # 2-level folder->feed, type="rss", htmlUrl present, and an empty folder.
        body = ET.fromstring(
            '<body>'
            '<outline text="Apps" title="Apps">'
            '<outline text="AppShopper" type="rss" xmlUrl="http://a/rss" htmlUrl="http://a"/>'
            '<outline text="OSX" type="rss" xmlUrl="http://b/rss" htmlUrl="http://b"/>'
            '</outline>'
            '<outline text="AWS" title="AWS"/>'  # empty folder
            '<outline text="Blog"><outline text="Arch" type="rss" xmlUrl="http://c/rss"/></outline>'
            '</body>'
        )
        out = _collect_feed_outlines(body)
        assert {(o.get("xmlUrl"), f) for o, f in out} == {
            ("http://a/rss", "Apps"),
            ("http://b/rss", "Apps"),
            ("http://c/rss", "Blog"),
        }

    def test_inoreader_duplicate_feed_across_folders(self):
        # Inoreader lets one feed live in multiple folders, so it appears twice in
        # the OPML. Both outlines are collected (import dedups via "Already
        # subscribed"); the structural parser must not silently drop either.
        body = ET.fromstring(
            '<body>'
            '<outline text="News"><outline text="WSJ" type="rss" xmlUrl="http://wsj/rss"/></outline>'
            '<outline text="Finance"><outline text="WSJ" type="rss" xmlUrl="http://wsj/rss"/></outline>'
            '</body>'
        )
        out = _collect_feed_outlines(body)
        assert sorted(f for _, f in out) == ["Finance", "News"]
        assert {o.get("xmlUrl") for o, _ in out} == {"http://wsj/rss"}


# ── _find_section ─────────────────────────────────────────────────────────────

class TestFindSection:
    def test_finds_named_section(self):
        body = ET.fromstring('<body><outline text="tt-rss-labels"/></body>')
        assert _find_section(body, "tt-rss-labels") is not None

    def test_missing_section_returns_none(self):
        body = ET.fromstring('<body><outline text="other"/></body>')
        assert _find_section(body, "tt-rss-labels") is None


# ── _parse_ttrss_filter ───────────────────────────────────────────────────────

class TestParseTtrssFilter:
    def test_basic_contains_and_mark_read(self):
        fd = {
            "name": "F1",
            "rules": [{"filter_type": "1", "reg_exp": "spam"}],
            "actions": [{"action_id": "2"}],
        }
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload is not None
        assert len(payload.conditions) == 1
        assert payload.conditions[0].field == "title"
        assert payload.conditions[0].operator == "contains"
        assert payload.actions[0].action_type == "mark_read"

    def test_match_any_rule_maps_to_or(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "x"}], "match_any_rule": True,
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload.match_operator == "OR"

    def test_inverse_rule_maps_to_not_contains(self):
        fd = {"rules": [{"filter_type": "5", "reg_exp": "x", "inverse": True}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload.conditions[0].field == "content"
        assert payload.conditions[0].operator == "not_contains"

    def test_regex_value_maps_to_regex_operator(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "foo.*bar"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload.conditions[0].operator == "regex"

    def test_unknown_filter_type_warns_and_skips_rule(self):
        res = ImportResult()
        fd = {"name": "F", "rules": [{"filter_type": "99", "reg_exp": "x"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, res)
        assert payload is None  # no valid conditions
        assert any("unknown filter_type" in w for w in res.warnings)

    def test_label_action_resolves_name_to_id(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "x"}],
              "actions": [{"action_id": "7", "action_param": "Tech"}]}
        payload = _parse_ttrss_filter(fd, {"Tech": 42}, {}, {}, ImportResult())
        assert payload.actions[0].action_type == "label"
        assert payload.actions[0].action_value == "42"

    def test_label_action_unknown_label_warns_and_skips(self):
        res = ImportResult()
        fd = {"name": "F", "rules": [{"filter_type": "1", "reg_exp": "x"}],
              "actions": [{"action_id": "7", "action_param": "Ghost"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, res)
        assert payload.actions == []
        assert any("not found" in w for w in res.warnings)

    def test_feed_scope_resolved_by_name(self):
        # All rules scoped to the same feed title → factored into scope_include.
        res = ImportResult()
        fd = {"name": "F", "match_any_rule": True,
              "rules": [{"filter_type": "1", "reg_exp": "a", "feed": "Zero Hedge"},
                        {"filter_type": "1", "reg_exp": "b", "feed": "Zero Hedge"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {"Zero Hedge": 7}, {}, res)
        assert payload.scope_include == ["feed:7"]
        assert res.warnings == []

    def test_category_scope_resolved_to_folder(self):
        # cat_filter=True targets a TTRSS category → Readfine folder.
        res = ImportResult()
        fd = {"name": "F",
              "rules": [{"filter_type": "1", "reg_exp": "x", "cat_filter": True,
                         "feed": "Search"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {"Search": 3}, res)
        assert payload.scope_include == ["folder:3"]

    def test_union_of_distinct_feed_scopes(self):
        res = ImportResult()
        fd = {"name": "F", "match_any_rule": True,
              "rules": [{"filter_type": "1", "reg_exp": ".*", "feed": "A"},
                        {"filter_type": "1", "reg_exp": ".*", "feed": "B"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {"A": 1, "B": 2}, {}, res)
        assert payload.scope_include == ["feed:1", "feed:2"]

    def test_mixed_scoped_and_global_imported_as_global(self):
        # A filter mixing a scoped rule with a global one must NOT narrow → global + warn.
        res = ImportResult()
        fd = {"name": "F", "match_any_rule": True,
              "rules": [{"filter_type": "3", "reg_exp": "brain", "feed": ""},
                        {"filter_type": "4", "reg_exp": "/medical/", "feed": "Zero Hedge"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {"Zero Hedge": 7}, {}, res)
        assert payload.scope_include == []
        assert any("mixes" in w for w in res.warnings)

    def test_unresolved_scope_warns_and_imports_global(self):
        res = ImportResult()
        fd = {"name": "F",
              "rules": [{"filter_type": "1", "reg_exp": "x", "feed": "Ghost Feed"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, res)
        assert payload.scope_include == []
        assert any("Ghost Feed" in w for w in res.warnings)

    def test_unscoped_filter_has_no_scope_and_no_warning(self):
        res = ImportResult()
        fd = {"name": "F", "rules": [{"filter_type": "1", "reg_exp": "x", "feed": ""}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, res)
        assert payload.scope_include == []
        assert res.warnings == []

    def test_watch_style_scoped_rules_collapse_to_one_condition(self):
        # The real-world "Watch" case: N rules, each a match-all (.*) scoped to a
        # different feed. After scope is factored to the filter, the conditions are
        # identical and collapse to one, while the scope keeps every feed.
        res = ImportResult()
        fd = {"name": "Watch", "match_any_rule": True,
              "rules": [{"filter_type": "1", "reg_exp": ".*", "feed": "A"},
                        {"filter_type": "1", "reg_exp": ".*", "feed": "B"},
                        {"filter_type": "1", "reg_exp": ".*", "feed": "C"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {"A": 1, "B": 2, "C": 3}, {}, res)
        payload.conditions = _dedupe_conditions(payload.conditions)
        assert len(payload.conditions) == 1
        assert payload.conditions[0].value == ".*"
        assert payload.scope_include == ["feed:1", "feed:2", "feed:3"]

    def test_distinct_conditions_are_preserved(self):
        # Different keywords must survive dedup (only exact triples collapse).
        res = ImportResult()
        fd = {"name": "Crypto", "match_any_rule": True,
              "rules": [{"filter_type": "3", "reg_exp": "bitcoin", "cat_filter": True, "feed": "Search"},
                        {"filter_type": "3", "reg_exp": "ethereum", "cat_filter": True, "feed": "Search"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {"Search": 5}, res)
        payload.conditions = _dedupe_conditions(payload.conditions)
        assert {c.value for c in payload.conditions} == {"bitcoin", "ethereum"}
        assert payload.scope_include == ["folder:5"]

    def test_dedupe_conditions_keeps_first_drops_repeats(self):
        conds = [
            FilterConditionCreate(field="title", operator="regex", value=".*", position=0),
            FilterConditionCreate(field="title", operator="regex", value=".*", position=1),
            FilterConditionCreate(field="content", operator="contains", value="x", position=2),
        ]
        out = _dedupe_conditions(conds)
        assert [(c.field, c.operator, c.value, c.position) for c in out] == [
            ("title", "regex", ".*", 0),
            ("content", "contains", "x", 2),
        ]

    def test_integer_filter_type_and_action_id(self):
        # Current TTRSS exports raw DB values without (int) cast on some backends,
        # so filter_type / action_id can arrive as JSON numbers, not strings.
        fd = {
            "name": "F",
            "rules": [{"filter_type": 1, "reg_exp": "spam"}],
            "actions": [{"action_id": 2}],
        }
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload is not None
        assert payload.conditions[0].field == "title"
        assert payload.actions[0].action_type == "mark_read"

    def test_postgres_bool_strings_for_flags(self):
        # Postgres via PDO serializes booleans as "t"/"f"; plain bool("f") is True,
        # so these flags must be normalized, not truthiness-tested.
        fd = {
            "rules": [{"filter_type": "1", "reg_exp": "x", "inverse": "f"}],
            "match_any_rule": "f",
            "enabled": "f",
            "actions": [{"action_id": "2"}],
        }
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload.match_operator == "AND"
        assert payload.is_active is False
        assert payload.conditions[0].operator == "contains"  # inverse "f" => not inverted

    def test_inverse_true_string_inverts(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "x", "inverse": "t"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {}, {}, ImportResult())
        assert payload.conditions[0].operator == "not_contains"

    def test_modern_match_scope_resolved_by_name(self):
        # Newer TTRSS "match" array: [[name, is_cat, is_zero], ...], referenced by name.
        res = ImportResult()
        fd = {"name": "F",
              "rules": [{"filter_type": "1", "reg_exp": "x",
                         "match": [["Tech", False, False], ["News", True, False]]}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {"Tech": 9}, {"News": 4}, res)
        assert payload.scope_include == ["feed:9", "folder:4"]

    def test_modern_match_all_entry_is_global(self):
        # An "all feeds" entry (feed id 0 / is_zero) makes the rule global → no scope.
        res = ImportResult()
        fd = {"name": "F",
              "rules": [{"filter_type": "1", "reg_exp": "x",
                         "match": [[0, False, True]]}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, {"Tech": 9}, {}, res)
        assert payload.scope_include == []
        assert res.warnings == []


# ── _parse_readfine_filter ────────────────────────────────────────────────────

class TestParseReadfineFilter:
    def test_roundtrip_shape(self):
        fd = {
            "name": "F",
            "enabled": True,
            "match_operator": "OR",
            "stop_on_match": True,
            "conditions": [{"field": "title", "operator": "contains", "value": "x", "position": 0}],
            "actions": [{"action_type": "star", "action_value": None}],
            "scope_include": [],
            "scope_except": [],
        }
        payload = _parse_readfine_filter(fd, {}, {}, {}, ImportResult())
        assert payload.match_operator == "OR"
        assert payload.stop_on_match is True
        assert payload.conditions[0].value == "x"
        assert payload.actions[0].action_type == "star"

    def test_label_name_resolved_to_id(self):
        fd = {
            "match_operator": "AND",
            "conditions": [{"field": "title", "operator": "contains", "value": "x"}],
            "actions": [{"action_type": "label", "action_value": "Tech"}],
        }
        payload = _parse_readfine_filter(fd, {"Tech": 7}, {}, {}, ImportResult())
        assert payload.actions[0].action_value == "7"

    def test_label_missing_skips_action(self):
        res = ImportResult()
        fd = {
            "match_operator": "AND",
            "conditions": [{"field": "title", "operator": "contains", "value": "x"}],
            "actions": [{"action_type": "label", "action_value": "Ghost"}],
        }
        payload = _parse_readfine_filter(fd, {}, {}, {}, res)
        assert payload.actions == []
        assert any("not found" in w for w in res.warnings)


# ── scope round-trip ──────────────────────────────────────────────────────────

class TestScope:
    def test_scope_to_urls_feed_and_folder(self):
        out = _scope_to_urls(
            json.dumps(["feed:1", "folder:2", "folder:0"]),
            {1: "http://a.com/rss"},
            {2: "Tech"},
        )
        assert out == ["feed:http://a.com/rss", "folder:Tech", "folder:__no_folder__"]

    def test_resolve_scope_back_to_ids(self):
        res = ImportResult()
        out = _resolve_scope(
            ["feed:http://a.com/rss", "folder:Tech", "folder:__no_folder__"],
            {"http://a.com/rss": 1},
            {"Tech": 2},
            res,
        )
        assert out == ["feed:1", "folder:2", "folder:0"]

    def test_resolve_scope_unknown_warns(self):
        res = ImportResult()
        out = _resolve_scope(["feed:http://gone.com/rss"], {}, {}, res)
        assert out == []
        assert any("not found" in w for w in res.warnings)

    def test_scope_to_urls_invalid_json_returns_empty(self):
        assert _scope_to_urls("not json", {}, {}) == []


# ── import_opml input validation ──────────────────────────────────────────────

class TestImportValidation:
    async def test_invalid_xml_raises(self):
        with pytest.raises(ValueError, match="Invalid OPML"):
            await import_opml(
                user=AsyncMock(), xml_bytes=b"<opml><not closed",
                import_feeds=True, import_labels=True, import_prefs=True,
                import_filters=True, db=AsyncMock(),
            )

    async def test_missing_body_raises(self):
        with pytest.raises(ValueError, match="no <body>"):
            await import_opml(
                user=AsyncMock(), xml_bytes=b'<opml version="2.0"><head/></opml>',
                import_feeds=True, import_labels=True, import_prefs=True,
                import_filters=True, db=AsyncMock(),
            )


# ── Scrape-feed OPML round-trip (export attrs ↔ import routing) ────────────────

class TestScrapeFeedRoundTrip:
    def test_export_emits_scrape_attrs(self):
        parent = Element("body")
        feed = SimpleNamespace(
            title="Forbes News",
            feed_url="https://www.forbes.com/news/",
            site_url="https://www.forbes.com/",
            feed_type="scrape",
            type_config={"article_links_selector": "a.headline"},
        )
        _feed_outline(parent, SimpleNamespace(custom_title=None), feed)

        outline = list(parent)[0]
        assert outline.get("feed-type") == "scrape"
        assert outline.get("article-links-selector") == "a.headline"
        assert outline.get("xmlUrl") == "https://www.forbes.com/news/"
        assert outline.get("htmlUrl") == "https://www.forbes.com/"

    def test_export_rss_feed_has_no_scrape_attrs(self):
        parent = Element("body")
        feed = SimpleNamespace(
            title="Example",
            feed_url="https://example.com/rss",
            site_url=None,
            feed_type="rss",
            type_config=None,
        )
        _feed_outline(parent, SimpleNamespace(custom_title=None), feed)

        outline = list(parent)[0]
        assert outline.get("feed-type") is None
        assert outline.get("article-links-selector") is None
        assert outline.get("xmlUrl") == "https://example.com/rss"

    async def test_import_routes_scrape_to_subscribe_scrape(self, monkeypatch):
        captured: dict = {}

        async def fake_scrape(**kwargs):
            captured.update(kwargs)
            return SimpleNamespace(feed_id=42)

        async def fake_subscribe(**kwargs):
            raise AssertionError("RSS subscribe must not be called for a scrape feed")

        monkeypatch.setattr("app.services.opml.subscribe_scrape", fake_scrape)
        monkeypatch.setattr("app.services.opml.subscribe", fake_subscribe)

        outline = Element("outline", {
            "xmlUrl": "https://www.forbes.com/news/",
            "text": "Forbes News",
            "feed-type": "scrape",
            "article-links-selector": "a.headline",
        })
        result = ImportResult()
        feed_id = await _import_feed(SimpleNamespace(), outline, None, result, db=None)

        assert feed_id == 42
        assert result.feeds_added == 1
        assert captured["url"] == "https://www.forbes.com/news/"
        assert captured["selector"] == "a.headline"
        # Variant 1: trust the backup, no live validation on restore.
        assert captured["validate_selector"] is False

    async def test_import_scrape_missing_selector_fails(self, monkeypatch):
        async def fake_scrape(**kwargs):
            raise AssertionError("subscribe_scrape must not run without a selector")

        monkeypatch.setattr("app.services.opml.subscribe_scrape", fake_scrape)

        outline = Element("outline", {
            "xmlUrl": "https://www.forbes.com/news/",
            "text": "Forbes News",
            "feed-type": "scrape",
        })
        result = ImportResult()
        feed_id = await _import_feed(SimpleNamespace(), outline, None, result, db=None)

        assert feed_id is None
        assert result.feeds_failed == 1

    async def test_import_routes_rss_to_subscribe(self, monkeypatch):
        async def fake_subscribe(**kwargs):
            return SimpleNamespace(feed_id=7)

        async def fake_scrape(**kwargs):
            raise AssertionError("scrape subscribe must not be called for an RSS feed")

        monkeypatch.setattr("app.services.opml.subscribe", fake_subscribe)
        monkeypatch.setattr("app.services.opml.subscribe_scrape", fake_scrape)

        outline = Element("outline", {"xmlUrl": "https://example.com/rss", "text": "Example"})
        result = ImportResult()
        feed_id = await _import_feed(SimpleNamespace(), outline, None, result, db=None)

        assert feed_id == 7
        assert result.feeds_added == 1

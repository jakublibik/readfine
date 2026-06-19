"""Unit tests for OPML import parsing/mapping (pure functions, no DB) plus
import_opml input validation. The DB-driven import paths are covered indirectly;
here we lock down the TTRSS↔Readfine mapping and structural parsing.
"""
import json
from unittest.mock import AsyncMock

import defusedxml.ElementTree as ET
import pytest

from app.services.opml import (
    ImportResult,
    _collect_feed_outlines,
    _find_section,
    _looks_like_regex,
    _parse_readfine_filter,
    _parse_ttrss_filter,
    _resolve_scope,
    _scope_to_urls,
    import_opml,
)


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
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
        assert payload is not None
        assert len(payload.conditions) == 1
        assert payload.conditions[0].field == "title"
        assert payload.conditions[0].operator == "contains"
        assert payload.actions[0].action_type == "mark_read"

    def test_match_any_rule_maps_to_or(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "x"}], "match_any_rule": True,
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
        assert payload.match_operator == "OR"

    def test_inverse_rule_maps_to_not_contains(self):
        fd = {"rules": [{"filter_type": "5", "reg_exp": "x", "inverse": True}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
        assert payload.conditions[0].field == "content"
        assert payload.conditions[0].operator == "not_contains"

    def test_regex_value_maps_to_regex_operator(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "foo.*bar"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
        assert payload.conditions[0].operator == "regex"

    def test_unknown_filter_type_warns_and_skips_rule(self):
        res = ImportResult()
        fd = {"name": "F", "rules": [{"filter_type": "99", "reg_exp": "x"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, res)
        assert payload is None  # no valid conditions
        assert any("unknown filter_type" in w for w in res.warnings)

    def test_label_action_resolves_name_to_id(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "x"}],
              "actions": [{"action_id": "7", "action_param": "Tech"}]}
        payload = _parse_ttrss_filter(fd, {"Tech": 42}, ImportResult())
        assert payload.actions[0].action_type == "label"
        assert payload.actions[0].action_value == "42"

    def test_label_action_unknown_label_warns_and_skips(self):
        res = ImportResult()
        fd = {"name": "F", "rules": [{"filter_type": "1", "reg_exp": "x"}],
              "actions": [{"action_id": "7", "action_param": "Ghost"}]}
        payload = _parse_ttrss_filter(fd, {}, res)
        assert payload.actions == []
        assert any("not found" in w for w in res.warnings)

    def test_per_id_scope_warns(self):
        res = ImportResult()
        fd = {"name": "F", "rules": [{"filter_type": "1", "reg_exp": "x", "feed_id": 5}],
              "actions": [{"action_id": "2"}]}
        _parse_ttrss_filter(fd, {}, res)
        assert any("scope" in w.lower() for w in res.warnings)

    def test_integer_filter_type_and_action_id(self):
        # Current TTRSS exports raw DB values without (int) cast on some backends,
        # so filter_type / action_id can arrive as JSON numbers, not strings.
        fd = {
            "name": "F",
            "rules": [{"filter_type": 1, "reg_exp": "spam"}],
            "actions": [{"action_id": 2}],
        }
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
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
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
        assert payload.match_operator == "AND"
        assert payload.is_active is False
        assert payload.conditions[0].operator == "contains"  # inverse "f" => not inverted

    def test_inverse_true_string_inverts(self):
        fd = {"rules": [{"filter_type": "1", "reg_exp": "x", "inverse": "t"}],
              "actions": [{"action_id": "2"}]}
        payload = _parse_ttrss_filter(fd, {}, ImportResult())
        assert payload.conditions[0].operator == "not_contains"

    def test_modern_match_scope_warns(self):
        res = ImportResult()
        fd = {"name": "F",
              "rules": [{"filter_type": "1", "reg_exp": "x",
                         "match": [["Tech", False, False]]}],
              "actions": [{"action_id": "2"}]}
        _parse_ttrss_filter(fd, {}, res)
        assert any("scope" in w.lower() for w in res.warnings)


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

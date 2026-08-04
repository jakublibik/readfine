"""Unit tests for filter condition evaluation logic."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from unittest.mock import AsyncMock, MagicMock

from app.services.filter_service import (
    _execute_actions,
    _matches_condition,
    _validate_ai_conditions,
    _validate_published_at_conditions,
    evaluate_filter,
    is_ai_filter,
)


def make_article(**kwargs):
    defaults = {
        "id": 1,
        "feed_id": 10,
        "title": "Test Article",
        "content": "Some content here",
        "author": "John Doe",
        "url": "https://example.com/article",
        "published_at": datetime(2024, 6, 1, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_user_feed(**kwargs):
    defaults = {"feed_id": 10, "user_id": 1, "folder_id": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_condition(field, operator, value, position=0):
    return SimpleNamespace(field=field, operator=operator, value=value, position=position)


def make_filter(conditions, match_operator="AND", is_active=True, stop_on_match=False,
                scope_include=None, scope_except=None):
    return SimpleNamespace(
        conditions=conditions,
        actions=[],
        match_operator=match_operator,
        is_active=is_active,
        stop_on_match=stop_on_match,
        scope_include=scope_include,
        scope_except=scope_except,
    )


# ── _matches_condition ────────────────────────────────────────────────────────

class TestContains:
    def test_matches_case_insensitive(self):
        article = make_article(title="Breaking News Today")
        cond = make_condition("title", "contains", "news")
        assert _matches_condition(cond, article, None) is True

    def test_no_match(self):
        article = make_article(title="Weather Report")
        cond = make_condition("title", "contains", "news")
        assert _matches_condition(cond, article, None) is False

    def test_content_field(self):
        article = make_article(content="Python is great for web development")
        cond = make_condition("content", "contains", "python")
        assert _matches_condition(cond, article, None) is True


class TestTitleOrContent:
    def test_matches_title(self):
        article = make_article(title="Python News", content="Nothing relevant")
        cond = make_condition("title_or_content", "contains", "python")
        assert _matches_condition(cond, article, None) is True

    def test_matches_content(self):
        article = make_article(title="Daily Digest", content="Python is great")
        cond = make_condition("title_or_content", "contains", "python")
        assert _matches_condition(cond, article, None) is True

    def test_matches_both(self):
        article = make_article(title="Python News", content="Python is great")
        cond = make_condition("title_or_content", "contains", "python")
        assert _matches_condition(cond, article, None) is True

    def test_no_match(self):
        article = make_article(title="Weather Report", content="Sunny skies today")
        cond = make_condition("title_or_content", "contains", "python")
        assert _matches_condition(cond, article, None) is False

    def test_not_contains_neither(self):
        article = make_article(title="Weather Report", content="Sunny skies")
        cond = make_condition("title_or_content", "not_contains", "python")
        assert _matches_condition(cond, article, None) is True

    def test_not_contains_in_title_returns_false(self):
        article = make_article(title="Python News", content="Sunny skies")
        cond = make_condition("title_or_content", "not_contains", "python")
        assert _matches_condition(cond, article, None) is False

    def test_not_contains_in_content_returns_false(self):
        article = make_article(title="Weather Report", content="Python is great")
        cond = make_condition("title_or_content", "not_contains", "python")
        assert _matches_condition(cond, article, None) is False


class TestNotContains:
    def test_no_match_returns_true(self):
        article = make_article(title="Weather Report")
        cond = make_condition("title", "not_contains", "news")
        assert _matches_condition(cond, article, None) is True

    def test_match_returns_false(self):
        article = make_article(title="Breaking News")
        cond = make_condition("title", "not_contains", "news")
        assert _matches_condition(cond, article, None) is False

    def test_none_field_returns_true(self):
        article = make_article(author=None)
        cond = make_condition("author", "not_contains", "spam")
        assert _matches_condition(cond, article, None) is True


class TestEquals:
    def test_exact_match(self):
        article = make_article(author="John Doe")
        cond = make_condition("author", "equals", "John Doe")
        assert _matches_condition(cond, article, None) is True

    def test_case_sensitive(self):
        article = make_article(author="John Doe")
        cond = make_condition("author", "equals", "john doe")
        assert _matches_condition(cond, article, None) is False



class TestRegex:
    def test_valid_pattern_matches(self):
        article = make_article(title="Python 3.12 Released")
        cond = make_condition("title", "regex", r"Python \d+\.\d+")
        assert _matches_condition(cond, article, None) is True

    def test_valid_pattern_no_match(self):
        article = make_article(title="Weather Report")
        cond = make_condition("title", "regex", r"Python \d+\.\d+")
        assert _matches_condition(cond, article, None) is False

    def test_invalid_regex_returns_false(self):
        article = make_article(title="Test")
        cond = make_condition("title", "regex", "[invalid")
        assert _matches_condition(cond, article, None) is False

    def test_case_insensitive(self):
        article = make_article(title="BREAKING NEWS")
        cond = make_condition("title", "regex", "breaking")
        assert _matches_condition(cond, article, None) is True

    def test_catastrophic_pattern_times_out_instead_of_hanging(self):
        # A catastrophic-backtracking pattern that bypasses the create-time
        # heuristic must not freeze evaluation: it is capped by the per-match
        # timeout and treated as "no match" rather than hanging the event loop.
        import time
        from app.services.filter_service import _REGEX_MATCH_TIMEOUT_S
        article = make_article(title="a" * 60 + "!")
        cond = make_condition("title", "regex", r"(a|a|a)+$")
        start = time.monotonic()
        result = _matches_condition(cond, article, None)
        elapsed = time.monotonic() - start
        assert result is False
        # Bounded by the timeout (plus slack for check granularity), not exponential:
        # this pattern would run for years unbounded.
        assert elapsed < _REGEX_MATCH_TIMEOUT_S * 2

    def test_large_input_still_matches(self):
        # The input cap is a safety net far above any real article; a legitimate
        # match near the start of a large body still succeeds.
        article = make_article(content="Breaking: " + "x" * 500_000)
        cond = make_condition("content", "regex", r"^Breaking")
        assert _matches_condition(cond, article, None) is True


class TestValidatePublishedAt:
    def test_valid_date(self):
        _validate_published_at_conditions([make_condition("published_at", "gt", "2024-06-15")])

    def test_invalid_date_text(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            _validate_published_at_conditions([make_condition("published_at", "gt", "tomorrow")])

    def test_invalid_date_with_time(self):
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            _validate_published_at_conditions([make_condition("published_at", "gt", "2024-06-15T14:00")])

    def test_non_published_at_fields_ignored(self):
        _validate_published_at_conditions([make_condition("title", "contains", "not-a-date")])


class TestGtLtDatetime:
    def test_gt_strictly_after_date(self):
        # published ON 2024-06-15 — gt should NOT match (strict)
        article = make_article(published_at=datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc))
        cond = make_condition("published_at", "gt", "2024-06-15")
        assert _matches_condition(cond, article, None) is False

    def test_gt_day_after_matches(self):
        article = make_article(published_at=datetime(2024, 6, 16, 0, 0, tzinfo=timezone.utc))
        cond = make_condition("published_at", "gt", "2024-06-15")
        assert _matches_condition(cond, article, None) is True

    def test_lt_strictly_before_date(self):
        # published ON 2024-06-15 — lt should NOT match (strict)
        article = make_article(published_at=datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc))
        cond = make_condition("published_at", "lt", "2024-06-15")
        assert _matches_condition(cond, article, None) is False

    def test_lt_day_before_matches(self):
        article = make_article(published_at=datetime(2024, 6, 14, 23, 59, tzinfo=timezone.utc))
        cond = make_condition("published_at", "lt", "2024-06-15")
        assert _matches_condition(cond, article, None) is True

    def test_invalid_date_returns_false(self):
        article = make_article(published_at=datetime(2024, 6, 15, tzinfo=timezone.utc))
        cond = make_condition("published_at", "gt", "not-a-date")
        assert _matches_condition(cond, article, None) is False


class TestEqualsDatetime:
    def test_date_only_matches_same_day(self):
        article = make_article(published_at=datetime(2024, 6, 15, 14, 30, tzinfo=timezone.utc))
        cond = make_condition("published_at", "equals", "2024-06-15")
        assert _matches_condition(cond, article, None) is True

    def test_date_only_different_day(self):
        article = make_article(published_at=datetime(2024, 6, 16, 0, 0, tzinfo=timezone.utc))
        cond = make_condition("published_at", "equals", "2024-06-15")
        assert _matches_condition(cond, article, None) is False

    def test_invalid_date_returns_false(self):
        article = make_article(published_at=datetime(2024, 6, 15, tzinfo=timezone.utc))
        cond = make_condition("published_at", "equals", "not-a-date")
        assert _matches_condition(cond, article, None) is False

    def test_none_published_at_returns_false(self):
        article = make_article(published_at=None)
        cond = make_condition("published_at", "equals", "2024-06-15")
        assert _matches_condition(cond, article, None) is False


class TestGtLt:
    def test_datetime_gt_numeric_content(self):
        article = make_article(published_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
        cond = make_condition("published_at", "gt", "2024-01-01")
        assert _matches_condition(cond, article, None) is True

    def test_datetime_lt_numeric_content(self):
        article = make_article(published_at=datetime(2023, 6, 1, tzinfo=timezone.utc))
        cond = make_condition("published_at", "lt", "2024-01-01")
        assert _matches_condition(cond, article, None) is True

    def test_datetime_gt(self):
        article = make_article(published_at=datetime(2024, 12, 1, tzinfo=timezone.utc))
        cond = make_condition("published_at", "gt", "2024-01-01")
        assert _matches_condition(cond, article, None) is True

    def test_datetime_lt(self):
        article = make_article(published_at=datetime(2023, 1, 1, tzinfo=timezone.utc))
        cond = make_condition("published_at", "lt", "2024-01-01")
        assert _matches_condition(cond, article, None) is True

    def test_invalid_value_returns_false(self):
        article = make_article(feed_id=10)
        cond = make_condition("feed_id", "gt", "not_a_number")
        assert _matches_condition(cond, article, None) is False




# ── evaluate_filter ───────────────────────────────────────────────────────────

class TestEvaluateFilter:
    def test_and_all_match(self):
        article = make_article(title="Python News", author="John")
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("author", "contains", "john"),
        ], match_operator="AND")
        assert evaluate_filter(f, article) is True

    def test_and_partial_match(self):
        article = make_article(title="Python News", author="Jane")
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("author", "equals", "John"),
        ], match_operator="AND")
        assert evaluate_filter(f, article) is False

    def test_or_one_match(self):
        article = make_article(title="Python News", author="Jane")
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("author", "equals", "John"),
        ], match_operator="OR")
        assert evaluate_filter(f, article) is True

    def test_or_none_match(self):
        article = make_article(title="Weather Report", author="Jane")
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("author", "equals", "John"),
        ], match_operator="OR")
        assert evaluate_filter(f, article) is False

    def test_empty_conditions_returns_false(self):
        article = make_article()
        f = make_filter([])
        assert evaluate_filter(f, article) is False

    def test_single_condition(self):
        article = make_article(title="Spam Message")
        f = make_filter([make_condition("title", "contains", "spam")])
        assert evaluate_filter(f, article) is True


class TestScope:
    import json as _json

    def test_scope_all_always_passes(self):
        f = make_filter([make_condition("title", "contains", "x")])
        # scope passes (include=all), but condition doesn't match
        assert evaluate_filter(f, make_article(title="no match")) is False

    # ── scope_include: feed ───────────────────────────────────────────────────

    def test_scope_include_feed_matches(self):
        import json
        article = make_article(feed_id=10, title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["feed:10"]))
        assert evaluate_filter(f, article) is True

    def test_scope_include_feed_no_match(self):
        import json
        article = make_article(feed_id=99, title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["feed:10"]))
        assert evaluate_filter(f, article) is False

    def test_scope_include_multiple_feeds(self):
        import json
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["feed:10", "feed:20"]))
        assert evaluate_filter(f, make_article(feed_id=10, title="Python")) is True
        assert evaluate_filter(f, make_article(feed_id=20, title="Python")) is True
        assert evaluate_filter(f, make_article(feed_id=99, title="Python")) is False

    # ── scope_include: folder ─────────────────────────────────────────────────

    def test_scope_include_folder_matches(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5"]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_include_folder_no_match(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=7)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5"]))
        assert evaluate_filter(f, article, uf) is False

    def test_scope_include_folder_no_user_feed(self):
        import json
        article = make_article(title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5"]))
        assert evaluate_filter(f, article, None) is False

    # ── folder:0 sentinel (no folder) ─────────────────────────────────────────

    def test_scope_include_folder0_matches_no_folder(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=None)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:0"]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_include_folder0_no_match_has_folder(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:0"]))
        assert evaluate_filter(f, article, uf) is False

    def test_scope_include_folder0_no_match_no_user_feed(self):
        import json
        article = make_article(title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:0"]))
        assert evaluate_filter(f, article, None) is False

    def test_scope_include_mixed_feed_and_folder(self):
        import json
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5", "feed:20"]))
        uf_in_folder = make_user_feed(feed_id=10, folder_id=5)
        uf_other = make_user_feed(feed_id=20, folder_id=7)
        assert evaluate_filter(f, make_article(feed_id=10, title="Python"), uf_in_folder) is True
        assert evaluate_filter(f, make_article(feed_id=20, title="Python"), uf_other) is True
        assert evaluate_filter(f, make_article(feed_id=99, title="Python"), make_user_feed(folder_id=9)) is False

    # ── scope_except ──────────────────────────────────────────────────────────

    def test_scope_except_excludes_feed(self):
        import json
        article = make_article(feed_id=10, title="Python")
        uf = make_user_feed(folder_id=None)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["feed:10"]))
        assert evaluate_filter(f, article, uf) is False

    def test_scope_except_does_not_exclude_other_feed(self):
        import json
        article = make_article(feed_id=99, title="Python")
        uf = make_user_feed(folder_id=None)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["feed:10"]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_except_excludes_folder(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["folder:5"]))
        assert evaluate_filter(f, article, uf) is False

    def test_scope_except_does_not_exclude_other_folder(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=7)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["folder:5"]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_except_folder0_excludes_no_folder(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=None)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["folder:0"]))
        assert evaluate_filter(f, article, uf) is False

    def test_scope_except_folder0_does_not_exclude_with_folder(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=3)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["folder:0"]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_except_multiple_entries(self):
        import json
        article = make_article(feed_id=10, title="Python")
        uf = make_user_feed(folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["feed:10", "folder:5"]))
        assert evaluate_filter(f, article, uf) is False

    def test_scope_except_invalid_json_ignored(self):
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=None)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except="not-valid-json")
        assert evaluate_filter(f, article, uf) is True

    def test_scope_except_malformed_entry_ignored(self):
        import json
        article = make_article(feed_id=10, title="Python")
        uf = make_user_feed(folder_id=None)
        # "feed:" without a number — should not raise, should be ignored
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps(["feed:", "folder:abc", "feed:10"]))
        assert evaluate_filter(f, article, uf) is False  # feed:10 still matches

    def test_scope_except_non_string_integers_ignored(self):
        import json
        article = make_article(feed_id=10, title="Python")
        uf = make_user_feed(folder_id=None)
        # [1] — integer items should not raise AttributeError
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps([1, 2, 3]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_except_null_items_ignored(self):
        import json
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=None)
        # [null] — None items should not raise
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps([None, None]))
        assert evaluate_filter(f, article, uf) is True

    def test_scope_except_mixed_types_string_still_applied(self):
        import json
        article = make_article(feed_id=10, title="Python")
        uf = make_user_feed(folder_id=None)
        # mix of non-strings and valid string — string entry should still work
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except=json.dumps([1, None, {}, "feed:10"]))
        assert evaluate_filter(f, article, uf) is False  # "feed:10" still excludes


# ── AI filters ────────────────────────────────────────────────────────────────

def make_state(ai_score=None, ai_filters_applied=False):
    return SimpleNamespace(ai_score=ai_score, ai_filters_applied=ai_filters_applied)


class TestIsAiFilter:
    def test_regular_filter(self):
        f = make_filter([make_condition("title", "contains", "python")])
        assert is_ai_filter(f) is False

    def test_ai_filter(self):
        f = make_filter([make_condition("ai_score", "gt", "70")])
        assert is_ai_filter(f) is True

    def test_mixed_filter_is_ai(self):
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("ai_score", "gt", "50"),
        ])
        assert is_ai_filter(f) is True

    def test_empty_conditions(self):
        f = make_filter([])
        assert is_ai_filter(f) is False


class TestValidateAiConditions:
    def test_valid_gt(self):
        _validate_ai_conditions([make_condition("ai_score", "gt", "70")])

    def test_valid_lt(self):
        _validate_ai_conditions([make_condition("ai_score", "lt", "30")])

    def test_valid_equals(self):
        _validate_ai_conditions([make_condition("ai_score", "equals", "50")])

    def test_valid_boundary_0(self):
        _validate_ai_conditions([make_condition("ai_score", "gt", "0")])

    def test_valid_boundary_100(self):
        _validate_ai_conditions([make_condition("ai_score", "lt", "100")])

    def test_invalid_operator_contains(self):
        with pytest.raises(ValueError, match="not allowed"):
            _validate_ai_conditions([make_condition("ai_score", "contains", "70")])

    def test_invalid_operator_regex(self):
        with pytest.raises(ValueError, match="not allowed"):
            _validate_ai_conditions([make_condition("ai_score", "regex", "70")])

    def test_value_above_100(self):
        with pytest.raises(ValueError, match="between 0 and 100"):
            _validate_ai_conditions([make_condition("ai_score", "gt", "101")])

    def test_value_below_0(self):
        with pytest.raises(ValueError, match="between 0 and 100"):
            _validate_ai_conditions([make_condition("ai_score", "lt", "-1")])

    def test_non_numeric_value(self):
        with pytest.raises(ValueError, match="must be a number"):
            _validate_ai_conditions([make_condition("ai_score", "gt", "high")])

    def test_non_ai_conditions_ignored(self):
        # Should not raise for regular fields
        _validate_ai_conditions([make_condition("title", "contains", "abc")])


class TestAiScoreCondition:
    def test_gt_match(self):
        article = make_article()
        state = make_state(ai_score=0.8)
        cond = make_condition("ai_score", "gt", "70")
        assert _matches_condition(cond, article, None, state) is True

    def test_gt_no_match(self):
        article = make_article()
        state = make_state(ai_score=0.5)
        cond = make_condition("ai_score", "gt", "70")
        assert _matches_condition(cond, article, None, state) is False

    def test_lt_match(self):
        article = make_article()
        state = make_state(ai_score=0.2)
        cond = make_condition("ai_score", "lt", "30")
        assert _matches_condition(cond, article, None, state) is True

    def test_lt_no_match(self):
        article = make_article()
        state = make_state(ai_score=0.5)
        cond = make_condition("ai_score", "lt", "30")
        assert _matches_condition(cond, article, None, state) is False

    def test_equals_match(self):
        article = make_article()
        state = make_state(ai_score=0.75)
        cond = make_condition("ai_score", "equals", "75.0")
        assert _matches_condition(cond, article, None, state) is True

    def test_no_state_returns_false_for_gt(self):
        article = make_article()
        cond = make_condition("ai_score", "gt", "50")
        assert _matches_condition(cond, article, None, None) is False

    def test_none_score_returns_false(self):
        article = make_article()
        state = make_state(ai_score=None)
        cond = make_condition("ai_score", "gt", "50")
        assert _matches_condition(cond, article, None, state) is False

    def test_boundary_gt_exact_value_not_matched(self):
        article = make_article()
        state = make_state(ai_score=0.7)
        # 0.7 * 100 = 70.0, gt 70 → False
        cond = make_condition("ai_score", "gt", "70")
        assert _matches_condition(cond, article, None, state) is False

    def test_boundary_lt_exact_value_not_matched(self):
        article = make_article()
        state = make_state(ai_score=0.3)
        cond = make_condition("ai_score", "lt", "30")
        assert _matches_condition(cond, article, None, state) is False


class TestEvaluateFilterWithAiScore:
    def test_ai_filter_matches(self):
        article = make_article()
        state = make_state(ai_score=0.85)
        f = make_filter([make_condition("ai_score", "gt", "80")])
        assert evaluate_filter(f, article, None, state) is True

    def test_ai_filter_no_match(self):
        article = make_article()
        state = make_state(ai_score=0.5)
        f = make_filter([make_condition("ai_score", "gt", "80")])
        assert evaluate_filter(f, article, None, state) is False

    def test_mixed_filter_and_both_match(self):
        article = make_article(title="Python")
        state = make_state(ai_score=0.9)
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("ai_score", "gt", "80"),
        ], match_operator="AND")
        assert evaluate_filter(f, article, None, state) is True

    def test_mixed_filter_and_title_fails(self):
        article = make_article(title="Weather")
        state = make_state(ai_score=0.9)
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("ai_score", "gt", "80"),
        ], match_operator="AND")
        assert evaluate_filter(f, article, None, state) is False

    def test_mixed_filter_and_score_fails(self):
        article = make_article(title="Python")
        state = make_state(ai_score=0.3)
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("ai_score", "gt", "80"),
        ], match_operator="AND")
        assert evaluate_filter(f, article, None, state) is False

    def test_mixed_filter_or_score_saves(self):
        # Title doesn't match but score does — OR → True
        article = make_article(title="Weather")
        state = make_state(ai_score=0.9)
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("ai_score", "gt", "80"),
        ], match_operator="OR")
        assert evaluate_filter(f, article, None, state) is True

    def test_mixed_filter_or_neither_matches(self):
        article = make_article(title="Weather")
        state = make_state(ai_score=0.2)
        f = make_filter([
            make_condition("title", "contains", "python"),
            make_condition("ai_score", "gt", "80"),
        ], match_operator="OR")
        assert evaluate_filter(f, article, None, state) is False

    def test_ai_filter_no_state_no_match(self):
        # AI filter without state → score is None → no match
        article = make_article()
        f = make_filter([make_condition("ai_score", "gt", "50")])
        assert evaluate_filter(f, article, None, None) is False

    # ── scope_include + scope_except combined ─────────────────────────────────

    def test_scope_include_with_except_excludes(self):
        import json
        # folder:5 included, but feed:10 excepted — feed:10 is in folder:5
        uf = make_user_feed(feed_id=10, folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5"]),
                        scope_except=json.dumps(["feed:10"]))
        assert evaluate_filter(f, make_article(feed_id=10, title="Python"), uf) is False

    def test_scope_include_with_except_allows_other(self):
        import json
        # folder:5 included, feed:10 excepted — feed:20 (also in folder:5) still passes
        uf20 = make_user_feed(feed_id=20, folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5"]),
                        scope_except=json.dumps(["feed:10"]))
        assert evaluate_filter(f, make_article(feed_id=20, title="Python"), uf20) is True

    def test_scope_include_not_matched_except_irrelevant(self):
        import json
        # feed:99 not in scope_include — should fail on include, not even reach except
        uf = make_user_feed(feed_id=99, folder_id=9)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include=json.dumps(["folder:5"]),
                        scope_except=json.dumps(["feed:10"]))
        assert evaluate_filter(f, make_article(feed_id=99, title="Python"), uf) is False

    # ── fail-closed / corrupt data ────────────────────────────────────────────

    def test_scope_include_corrupt_json_fail_closed(self):
        # Corrupt scope_include must NOT expand to all feeds — filter must not match
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include="not-valid-json")
        assert evaluate_filter(f, make_article(title="Python")) is False

    def test_scope_include_corrupt_json_fail_closed_regardless_of_condition(self):
        # Even if condition matches perfectly, corrupt scope_include → no match
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_include="{bad}")
        assert evaluate_filter(f, make_article(feed_id=10, title="Python")) is False

    def test_scope_except_corrupt_json_ignored(self):
        # Corrupt scope_except is treated as empty (fail-safe: nothing excluded)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_except="not-valid-json")
        assert evaluate_filter(f, make_article(title="Python")) is True


# ── _execute_actions: archive ───────────────────────────────────────────────

def make_action(action_type, action_value=None):
    return SimpleNamespace(action_type=action_type, action_value=action_value)


def _state_fetch_db(state):
    """Mock AsyncSession whose execute() returns `state` from scalar_one_or_none()."""
    db = AsyncMock()
    db.add = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=state)
    db.execute = AsyncMock(return_value=result)
    return db


class TestExecuteArchiveAction:
    async def test_archive_sets_is_archived(self):
        f = make_filter([])
        f.id = 1
        f.actions = [make_action("archive")]
        state = SimpleNamespace(is_read=False, is_starred=False, is_archived=False)
        db = _state_fetch_db(state)

        changed = await _execute_actions(f, make_article(), user_id=1, user_feed=make_user_feed(), db=db)

        assert changed is True
        assert state.is_archived is True
        # Archive must not touch read/star state.
        assert state.is_read is False
        assert state.is_starred is False

    async def test_archive_idempotent_when_already_archived(self):
        f = make_filter([])
        f.id = 1
        f.actions = [make_action("archive")]
        state = SimpleNamespace(is_read=False, is_starred=False, is_archived=True)
        db = _state_fetch_db(state)

        changed = await _execute_actions(f, make_article(), user_id=1, user_feed=make_user_feed(), db=db)

        assert changed is False
        assert state.is_archived is True

    async def test_archive_creates_state_when_missing(self):
        f = make_filter([])
        f.id = 1
        f.actions = [make_action("archive")]
        db = _state_fetch_db(None)  # no existing state row

        changed = await _execute_actions(f, make_article(), user_id=1, user_feed=make_user_feed(), db=db)

        assert changed is True
        db.add.assert_called_once()
        added = db.add.call_args[0][0]
        assert added.is_archived is True


# ── Saved articles: scope with no feed and no UserFeed ────────────────────────

class TestScopeWithoutAFeed:
    """A saved-by-URL article has feed_id=None and no UserFeed row, so scope tokens
    that resolve through either of those can never match."""

    def _saved_article(self):
        return make_article(feed_id=None)

    def test_unscoped_filter_matches(self):
        """Empty scope_include means 'All articles', which is how saved articles pick
        up a user's general rules."""
        f = make_filter([make_condition("title", "contains", "Test")], scope_include=None)
        assert evaluate_filter(f, self._saved_article(), None) is True

    def test_empty_list_scope_matches(self):
        f = make_filter([make_condition("title", "contains", "Test")], scope_include="[]")
        assert evaluate_filter(f, self._saved_article(), None) is True

    def test_feed_scoped_filter_does_not_match(self):
        f = make_filter([make_condition("title", "contains", "Test")],
                        scope_include='["feed:10"]')
        assert evaluate_filter(f, self._saved_article(), None) is False

    def test_folder_scoped_filter_does_not_match(self):
        f = make_filter([make_condition("title", "contains", "Test")],
                        scope_include='["folder:3"]')
        assert evaluate_filter(f, self._saved_article(), None) is False

    def test_no_folder_sentinel_does_not_match_either(self):
        f = make_filter([make_condition("title", "contains", "Test")],
                        scope_include='["folder:0"]')
        assert evaluate_filter(f, self._saved_article(), None) is False

    def test_folder_except_cannot_exclude_a_saved_article(self):
        """Corollary worth pinning down: folder:0 in scope_except resolves through
        user_feed, so it excludes nothing here."""
        f = make_filter([make_condition("title", "contains", "Test")],
                        scope_include="[]", scope_except='["folder:0"]')
        assert evaluate_filter(f, self._saved_article(), None) is True

    async def test_actions_execute_without_a_user_feed(self):
        f = make_filter([])
        f.id = 1
        f.actions = [make_action("star")]
        state = SimpleNamespace(is_read=False, is_starred=False, is_archived=True,
                                ever_starred=False, starred_at=None)
        db = _state_fetch_db(state)

        changed = await _execute_actions(
            f, self._saved_article(), user_id=1, user_feed=None, db=db
        )

        assert changed is True
        assert state.is_starred is True

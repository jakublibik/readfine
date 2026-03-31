"""Unit tests for filter condition evaluation logic."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.filter_service import _matches_condition, evaluate_filter


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

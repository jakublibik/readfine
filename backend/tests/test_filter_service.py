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
                scope_type="all", scope_feed_id=None, scope_folder_id=None, scope_except=None):
    return SimpleNamespace(
        conditions=conditions,
        actions=[],
        match_operator=match_operator,
        is_active=is_active,
        stop_on_match=stop_on_match,
        scope_type=scope_type,
        scope_feed_id=scope_feed_id,
        scope_folder_id=scope_folder_id,
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
    def test_scope_all_always_passes(self):
        article = make_article(feed_id=99)
        f = make_filter([make_condition("title", "contains", "x")], scope_type="all")
        # scope passes, but condition doesn't match
        assert evaluate_filter(f, make_article(title="no match")) is False

    def test_scope_feed_matches(self):
        article = make_article(feed_id=10, title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_type="feed", scope_feed_id=10)
        assert evaluate_filter(f, article) is True

    def test_scope_feed_no_match(self):
        article = make_article(feed_id=99, title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_type="feed", scope_feed_id=10)
        assert evaluate_filter(f, article) is False

    def test_scope_folder_matches(self):
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=5)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_type="folder", scope_folder_id=5)
        assert evaluate_filter(f, article, uf) is True

    def test_scope_folder_no_match(self):
        article = make_article(title="Python")
        uf = make_user_feed(folder_id=7)
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_type="folder", scope_folder_id=5)
        assert evaluate_filter(f, article, uf) is False

    def test_scope_folder_no_user_feed(self):
        article = make_article(title="Python")
        f = make_filter([make_condition("title", "contains", "python")],
                        scope_type="folder", scope_folder_id=5)
        assert evaluate_filter(f, article, None) is False

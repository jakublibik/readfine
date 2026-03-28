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


def make_filter(conditions, match_operator="AND", is_active=True, stop_on_match=False):
    return SimpleNamespace(
        conditions=conditions,
        actions=[],
        match_operator=match_operator,
        is_active=is_active,
        stop_on_match=stop_on_match,
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

    def test_feed_id_match(self):
        article = make_article(feed_id=42)
        cond = make_condition("feed_id", "equals", "42")
        assert _matches_condition(cond, article, None) is True


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
    def test_numeric_gt(self):
        article = make_article(feed_id=100)
        cond = make_condition("feed_id", "gt", "50")
        assert _matches_condition(cond, article, None) is True

    def test_numeric_lt(self):
        article = make_article(feed_id=10)
        cond = make_condition("feed_id", "lt", "50")
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


class TestFolderIdField:
    def test_folder_id_from_user_feed(self):
        article = make_article()
        uf = make_user_feed(folder_id=5)
        cond = make_condition("folder_id", "equals", "5")
        assert _matches_condition(cond, article, uf) is True

    def test_no_folder_returns_false(self):
        article = make_article()
        uf = make_user_feed(folder_id=None)
        cond = make_condition("folder_id", "equals", "5")
        assert _matches_condition(cond, article, uf) is False


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

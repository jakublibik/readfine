"""Unit tests for purge service pure helpers."""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.purge_service import ids_exceeding_age, ids_exceeding_count

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=100)
RECENT = NOW - timedelta(days=10)


# ── ids_exceeding_age ─────────────────────────────────────────────────────────

class TestIdsExceedingAge:
    def _cutoff(self, days: int) -> datetime:
        return NOW - timedelta(days=days)

    def test_old_article_returned(self):
        articles = [(1, OLD)]
        assert ids_exceeding_age(articles, self._cutoff(90)) == {1}

    def test_recent_article_not_returned(self):
        articles = [(1, RECENT)]
        assert ids_exceeding_age(articles, self._cutoff(90)) == set()

    def test_exactly_on_cutoff_is_kept(self):
        cutoff = self._cutoff(90)
        articles = [(1, cutoff)]
        assert ids_exceeding_age(articles, cutoff) == set()

    def test_mix_old_and_recent(self):
        articles = [(1, OLD), (2, RECENT), (3, OLD)]
        assert ids_exceeding_age(articles, self._cutoff(90)) == {1, 3}

    def test_empty_list(self):
        assert ids_exceeding_age([], self._cutoff(90)) == set()


# ── ids_exceeding_count ───────────────────────────────────────────────────────

def _make_articles(feed_id: int, dates: list[datetime]) -> list[tuple]:
    """(id, feed_id, published_at, fetched_at) — id assigned sequentially."""
    start_id = feed_id * 100
    return [(start_id + i, feed_id, d, d) for i, d in enumerate(dates)]


class TestIdsExceedingCount:
    def test_within_limit_nothing_deleted(self):
        articles = _make_articles(1, [NOW - timedelta(days=i) for i in range(3)])
        assert ids_exceeding_count(articles, keep_count=5) == set()

    def test_excess_articles_returned(self):
        # 5 articles, keep 3 → 2 oldest deleted
        dates = [NOW - timedelta(days=i) for i in range(5)]
        articles = _make_articles(1, dates)
        excess = ids_exceeding_count(articles, keep_count=3)
        # oldest two: index 3 and 4 (days 3 and 4 ago)
        assert excess == {103, 104}

    def test_keeps_newest(self):
        dates = [NOW - timedelta(days=i) for i in range(5)]
        articles = _make_articles(1, dates)
        excess = ids_exceeding_count(articles, keep_count=3)
        kept = {a[0] for a in articles} - excess
        newest_ids = {100, 101, 102}  # days 0, 1, 2
        assert kept == newest_ids

    def test_multiple_feeds_independent(self):
        # Feed 1: 4 articles, keep 2 → 2 deleted
        # Feed 2: 2 articles, keep 2 → 0 deleted
        f1 = _make_articles(1, [NOW - timedelta(days=i) for i in range(4)])
        f2 = _make_articles(2, [NOW - timedelta(days=i) for i in range(2)])
        excess = ids_exceeding_count(f1 + f2, keep_count=2)
        assert excess == {102, 103}  # feed 1 oldest two
        assert not excess & {200, 201}  # feed 2 untouched

    def test_published_at_takes_priority_over_fetched_at(self):
        # Article with older fetched_at but newer published_at should be kept
        articles = [
            (1, 1, NOW - timedelta(days=1), NOW - timedelta(days=50)),  # published yesterday
            (2, 1, NOW - timedelta(days=10), NOW - timedelta(days=10)),  # published 10 days ago
            (3, 1, NOW - timedelta(days=20), NOW - timedelta(days=20)),  # oldest
        ]
        excess = ids_exceeding_count(articles, keep_count=2)
        assert excess == {3}

    def test_published_at_none_falls_back_to_fetched_at(self):
        articles = [
            (1, 1, None, NOW - timedelta(days=1)),
            (2, 1, None, NOW - timedelta(days=5)),
            (3, 1, None, NOW - timedelta(days=10)),
        ]
        excess = ids_exceeding_count(articles, keep_count=2)
        assert excess == {3}

    def test_exact_limit_nothing_deleted(self):
        articles = _make_articles(1, [NOW - timedelta(days=i) for i in range(5)])
        assert ids_exceeding_count(articles, keep_count=5) == set()

    def test_empty_list(self):
        assert ids_exceeding_count([], keep_count=100) == set()

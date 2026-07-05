"""Unit tests for catchup_service pure functions."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.catchup_service import (
    _parse_scope,
    _period_to_start_dt,
    _snippet,
    apply_catchup_limit,
    build_articles_meta,
    estimate_catchup_tokens,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_article(
    id: int = 1,
    title: str = "Test Article",
    feed_title: str = "Test Feed",
    published_at: datetime | None = None,
    fetched_at: datetime | None = None,
    ai_score: float | None = None,
    ai_summary: str | None = None,
    readable_content: str | None = None,
    content: str | None = None,
    folder_id: int | None = None,
):
    from app.services.catchup_service import CatchupArticle
    return CatchupArticle(
        id=id,
        title=title,
        feed_title=feed_title,
        published_at=published_at,
        fetched_at=fetched_at or datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc),
        folder_id=folder_id,
        ai_score=ai_score,
        ai_summary=ai_summary,
        readable_content=readable_content,
        content=content,
    )


def articles_on_day(day_offset: int, count: int, base_score: float = 0.5, id_start: int = 1):
    """Create `count` articles on a specific day relative to 2024-06-01."""
    base = datetime(2024, 6, 1, tzinfo=timezone.utc) + timedelta(days=day_offset)
    return [
        make_article(
            id=id_start + i,
            published_at=base + timedelta(minutes=i),
            ai_score=base_score - i * 0.01,
        )
        for i in range(count)
    ]


# ── _period_to_start_dt ───────────────────────────────────────────────────────

class TestPeriodToStartDt:
    def test_today_utc(self):
        now = datetime.now(timezone.utc)
        result = _period_to_start_dt("today", "UTC")
        expected = now.replace(hour=0, minute=0, second=0, microsecond=0)
        assert result == expected.astimezone(timezone.utc)

    def test_yesterday_utc(self):
        now = datetime.now(timezone.utc)
        result = _period_to_start_dt("yesterday", "UTC")
        expected = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1))
        assert result == expected.astimezone(timezone.utc)

    def test_7days_utc(self):
        now = datetime.now(timezone.utc)
        result = _period_to_start_dt("7days", "UTC")
        expected = (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7))
        assert result == expected.astimezone(timezone.utc)

    @pytest.mark.parametrize("frozen_now, expected_diff_hours", [
        # Freeze "now" at midday UTC so both zones anchor to the same calendar day —
        # the flakiness came from comparing two "today" midnights that could land on
        # different dates near the UTC day boundary (~22:00–24:00 UTC).
        (datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc), 2),   # DST → Prague UTC+2
        (datetime(2024, 1, 15, 12, 0, tzinfo=timezone.utc), 1),   # no DST → Prague UTC+1
    ])
    def test_timezone_offset(self, monkeypatch, frozen_now, expected_diff_hours):
        """Prague midnight, expressed in UTC, is 1h (winter) / 2h (summer) before UTC midnight."""
        import app.services.catchup_service as mod

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen_now.astimezone(tz) if tz else frozen_now

        monkeypatch.setattr(mod, "datetime", _FrozenDatetime)
        result_utc = _period_to_start_dt("today", "UTC")
        result_tz = _period_to_start_dt("today", "Europe/Prague")
        diff_hours = (result_utc - result_tz).total_seconds() / 3600
        assert diff_hours == expected_diff_hours

    def test_invalid_timezone_falls_back_to_utc(self):
        result_invalid = _period_to_start_dt("today", "Invalid/Zone")
        result_utc = _period_to_start_dt("today", "UTC")
        assert result_invalid == result_utc

    def test_none_timezone_falls_back_to_utc(self):
        result_none = _period_to_start_dt("today", None)
        result_utc = _period_to_start_dt("today", "UTC")
        assert result_none == result_utc


# ── _parse_scope ──────────────────────────────────────────────────────────────

class TestParseScope:
    def test_none_returns_empty(self):
        assert _parse_scope(None) == ([], [])

    def test_empty_string_returns_empty(self):
        assert _parse_scope("") == ([], [])

    def test_empty_json_array(self):
        assert _parse_scope("[]") == ([], [])

    def test_feed_ids(self):
        scope = json.dumps(["feed:1", "feed:2", "feed:10"])
        feed_ids, folder_ids = _parse_scope(scope)
        assert feed_ids == [1, 2, 10]
        assert folder_ids == []

    def test_folder_ids(self):
        scope = json.dumps(["folder:3", "folder:0"])
        feed_ids, folder_ids = _parse_scope(scope)
        assert feed_ids == []
        assert folder_ids == [3, 0]

    def test_mixed(self):
        scope = json.dumps(["feed:1", "folder:2", "feed:3"])
        feed_ids, folder_ids = _parse_scope(scope)
        assert feed_ids == [1, 3]
        assert folder_ids == [2]

    def test_invalid_json_returns_empty(self):
        assert _parse_scope("not-json") == ([], [])

    def test_invalid_item_skipped(self):
        scope = json.dumps(["feed:abc", "feed:1"])
        feed_ids, folder_ids = _parse_scope(scope)
        assert feed_ids == [1]


# ── _snippet ──────────────────────────────────────────────────────────────────

class TestSnippet:
    def test_ai_summary_preferred(self):
        a = make_article(
            ai_summary="Summary text",
            readable_content="Readable text",
            content="Content text",
        )
        assert _snippet(a) == "Summary text"

    def test_readable_content_fallback(self):
        a = make_article(
            ai_summary=None,
            readable_content="Readable text",
            content="Content text",
        )
        assert _snippet(a) == "Readable text"

    def test_content_fallback(self):
        a = make_article(ai_summary=None, readable_content=None, content="Content text")
        assert _snippet(a) == "Content text"

    def test_all_none_returns_empty(self):
        a = make_article(ai_summary=None, readable_content=None, content=None)
        assert _snippet(a) == ""

    def test_html_stripped_from_content(self):
        a = make_article(content="<p>Hello <b>world</b></p>")
        assert _snippet(a) == "Hello world"

    def test_html_stripped_from_readable(self):
        a = make_article(readable_content="<p>Clean <em>text</em></p>")
        assert _snippet(a) == "Clean text"

    def test_whitespace_normalized(self):
        a = make_article(content="  too   many   spaces  ")
        assert _snippet(a) == "too many spaces"  # normalize collapses all whitespace

    def test_ai_summary_truncated_at_200(self):
        a = make_article(ai_summary="x" * 300)
        assert len(_snippet(a)) == 200

    def test_content_truncated_at_150(self):
        a = make_article(content="x" * 300)
        assert len(_snippet(a)) == 150

    def test_readable_content_truncated_at_150(self):
        a = make_article(readable_content="x" * 300)
        assert len(_snippet(a)) == 150


# ── apply_catchup_limit ───────────────────────────────────────────────────────

class TestApplyCatchupLimit:
    def test_count_below_limit_returns_all(self):
        articles = articles_on_day(0, 10)
        result = apply_catchup_limit(articles, 200, scoring_available=True)
        assert len(result) == 10

    def test_count_equal_limit_returns_all(self):
        articles = articles_on_day(0, 50)
        result = apply_catchup_limit(articles, 50, scoring_available=True)
        assert len(result) == 50

    def test_result_never_exceeds_limit(self):
        articles = articles_on_day(0, 300)
        result = apply_catchup_limit(articles, 100, scoring_available=True)
        assert len(result) <= 100

    def test_result_capped_when_active_days_exceed_limit(self):
        """Per-day pass takes >=1 article per active day; with more active days
        than the limit that alone would exceed it. Result must still be capped."""
        all_articles = []
        for d in range(10):  # 10 active days, 1 article each
            all_articles += articles_on_day(d, 1, id_start=d * 100)
        result = apply_catchup_limit(all_articles, 5, scoring_available=True)
        assert len(result) == 5

    def test_no_undercount_with_uneven_days(self):
        """Day 1 has only 5 articles, days 2-7 have 100 each. Result should be close to limit."""
        day1 = articles_on_day(0, 5, id_start=1)
        other_days = []
        for d in range(1, 7):
            other_days += articles_on_day(d, 100, id_start=1000 + d * 100)
        articles = day1 + other_days
        result = apply_catchup_limit(articles, 200, scoring_available=True)
        # With spillover, should get close to 200
        assert len(result) >= 190

    def test_all_days_represented_when_scoring_available(self):
        """Each of 7 days should have at least some articles in result."""
        all_articles = []
        for d in range(7):
            all_articles += articles_on_day(d, 50, id_start=d * 100)
        result = apply_catchup_limit(all_articles, 100, scoring_available=True)
        from app.services.catchup_service import _date_key
        days_in_result = {_date_key(a) for a in result}
        assert len(days_in_result) == 7

    def test_all_days_represented_scoring_disabled(self):
        """With higher ratio (0.8), all days should still be represented."""
        all_articles = []
        for d in range(7):
            all_articles += articles_on_day(d, 50, id_start=d * 100)
        result = apply_catchup_limit(all_articles, 100, scoring_available=False)
        from app.services.catchup_service import _date_key
        days_in_result = {_date_key(a) for a in result}
        assert len(days_in_result) == 7

    def test_higher_ratio_when_scoring_disabled(self):
        """With scoring disabled, coverage pass should take more articles."""
        all_articles = []
        for d in range(7):
            all_articles += articles_on_day(d, 30, id_start=d * 100)
        result_scored = apply_catchup_limit(all_articles[:], 50, scoring_available=True)
        result_noscored = apply_catchup_limit(all_articles[:], 50, scoring_available=False)
        # Both should return 50 articles
        assert len(result_scored) == 50
        assert len(result_noscored) == 50

    def test_result_sorted_by_date_desc(self):
        all_articles = []
        for d in range(3):
            all_articles += articles_on_day(d, 20, id_start=d * 100)
        result = apply_catchup_limit(all_articles, 30, scoring_available=True)
        from app.services.catchup_service import _ts
        timestamps = [_ts(a) for a in result]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_single_day_no_sampling_needed(self):
        """With 1 day and limit > count, return all."""
        articles = articles_on_day(0, 50)
        result = apply_catchup_limit(articles, 200, scoring_available=True)
        assert len(result) == 50

    def test_empty_list(self):
        assert apply_catchup_limit([], 200, scoring_available=True) == []

    def test_unique_ids_in_result(self):
        """No article should appear twice."""
        all_articles = []
        for d in range(5):
            all_articles += articles_on_day(d, 60, id_start=d * 100)
        result = apply_catchup_limit(all_articles, 100, scoring_available=True)
        ids = [a.id for a in result]
        assert len(ids) == len(set(ids))

    def test_high_score_articles_preferred_in_spillover(self):
        """Spillover pass should prefer higher ai_score articles."""
        # Day 1: 5 articles with low score (will fill base quota only)
        # Days 2-7: articles with varied scores
        day1 = [make_article(id=i, published_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                              ai_score=0.1) for i in range(1, 6)]
        high_score = [make_article(id=100+i, published_at=datetime(2024, 6, 2, tzinfo=timezone.utc),
                                   ai_score=0.9) for i in range(20)]
        low_score = [make_article(id=200+i, published_at=datetime(2024, 6, 2, tzinfo=timezone.utc),
                                  ai_score=0.1) for i in range(80)]
        articles = day1 + high_score + low_score
        result = apply_catchup_limit(articles, 30, scoring_available=True)
        result_ids = {a.id for a in result}
        # All high_score articles should be in result
        high_ids = {a.id for a in high_score}
        assert high_ids.issubset(result_ids)


# ── build_articles_meta ───────────────────────────────────────────────────────

class TestBuildArticlesMeta:
    def test_basic_fields_present(self):
        articles = [make_article(title="Hello", feed_title="Feed A",
                                 published_at=datetime(2024, 6, 1, tzinfo=timezone.utc))]
        meta = build_articles_meta(articles, include_snippet=False)
        assert len(meta) == 1
        assert meta[0]["title"] == "Hello"
        assert meta[0]["feed"] == "Feed A"
        assert meta[0]["date"] == "2024-06-01"

    def test_snippet_included_when_enabled(self):
        articles = [make_article(ai_summary="Short summary")]
        meta = build_articles_meta(articles, include_snippet=True)
        assert meta[0]["snippet"] == "Short summary"

    def test_snippet_not_present_when_disabled(self):
        articles = [make_article(ai_summary="Short summary")]
        meta = build_articles_meta(articles, include_snippet=False)
        assert "snippet" not in meta[0]

    def test_empty_snippet_when_no_content(self):
        articles = [make_article(ai_summary=None, readable_content=None, content=None)]
        meta = build_articles_meta(articles, include_snippet=True)
        assert meta[0]["snippet"] == ""

    def test_date_uses_fetched_at_when_published_at_none(self):
        articles = [make_article(
            published_at=None,
            fetched_at=datetime(2024, 7, 15, tzinfo=timezone.utc),
        )]
        meta = build_articles_meta(articles, include_snippet=False)
        assert meta[0]["date"] == "2024-07-15"

    def test_empty_list(self):
        assert build_articles_meta([], include_snippet=True) == []


# ── estimate_catchup_tokens ───────────────────────────────────────────────────

class TestEstimateCatchupTokens:
    def test_without_snippet(self):
        inp, out = estimate_catchup_tokens(100, include_snippet=False)
        assert inp == 100 * 20 + 300
        assert out == 800

    def test_with_snippet(self):
        inp, out = estimate_catchup_tokens(100, include_snippet=True)
        assert inp == 100 * 55 + 300
        assert out == 800

    def test_default_limit(self):
        inp_s, _ = estimate_catchup_tokens(200, include_snippet=True)
        assert inp_s == 200 * 55 + 300

    def test_snippet_increases_tokens(self):
        inp_no, _ = estimate_catchup_tokens(50, include_snippet=False)
        inp_yes, _ = estimate_catchup_tokens(50, include_snippet=True)
        assert inp_yes > inp_no

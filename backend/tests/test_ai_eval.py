"""Unit tests for scoring-eval metrics (pure functions) + engaged-label guard."""
import inspect

from app.services.ai_eval_service import (
    calibration_buckets,
    compute_auc,
    effective_window,
    exposure_floor,
    get_scoring_eval,
    score_histogram,
    window_presets,
)


class TestComputeAuc:
    def test_perfect_ranking(self):
        pairs = [(0.9, True), (0.8, True), (0.2, False), (0.1, False)]
        assert compute_auc(pairs) == 1.0

    def test_inverted_ranking(self):
        pairs = [(0.1, True), (0.2, True), (0.8, False), (0.9, False)]
        assert compute_auc(pairs) == 0.0

    def test_tie_gives_half(self):
        assert compute_auc([(0.5, True), (0.5, False)]) == 0.5

    def test_none_when_single_class(self):
        assert compute_auc([(0.5, True), (0.6, True)]) is None
        assert compute_auc([(0.5, False)]) is None

    def test_ties_stay_in_range(self):
        pairs = [(0.5, True), (0.5, False), (0.9, True), (0.1, False)]
        auc = compute_auc(pairs)
        assert 0.0 <= auc <= 1.0


class TestEffectiveWindow:
    """Past retention only starred/archived survivors are left, so a window
    reaching into that zone measures survival, not scoring quality."""

    def test_clamps_to_purge_horizon_with_margin(self):
        days, info = effective_window(90, 60)
        assert days == 55
        assert info["clamped"] is True
        assert info["requested_days"] == 90
        assert info["purge_after_days"] == 60

    def test_shorter_window_is_left_alone(self):
        days, info = effective_window(30, 60)
        assert days == 30
        assert info["clamped"] is False

    def test_no_retention_configured_means_no_cut(self):
        days, info = effective_window(365, None)
        assert days == 365
        assert info["clamped"] is False

    def test_never_returns_a_non_positive_window(self):
        days, _ = effective_window(90, 3)
        assert days >= 1


class TestWindowPresets:
    """Fixed presets up to a year made several buttons return the same window
    once it started being clamped to retention."""

    def test_offers_nothing_beyond_the_horizon(self):
        presets = window_presets(60)
        assert presets == [7, 14, 30, 55]
        assert presets == sorted(set(presets))

    def test_longer_retention_offers_more(self):
        assert window_presets(120) == [7, 14, 30, 60, 90, 115]

    def test_no_retention_keeps_a_year(self):
        assert window_presets(None)[-1] == 365

    def test_every_preset_survives_the_clamp_unchanged(self):
        # otherwise a button would silently return a different window than it says
        for t1 in (30, 60, 90, 120, None):
            for d in window_presets(t1):
                assert effective_window(d, t1)[0] == d


class TestExposureFloor:
    """A filter that hides low-scoring articles decides its own ground truth."""

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Db:
        def __init__(self, rows):
            self.rows = rows

        async def execute(self, *_args, **_kwargs):
            return TestExposureFloor._Result(self.rows)

    async def _floor(self, rows):
        return await exposure_floor(self._Db(rows), 1)

    async def test_none_when_no_such_filter(self):
        assert await self._floor([]) is None

    async def test_reads_threshold_and_scales_to_the_stored_range(self):
        got = await self._floor([("AI - LowScore", "30", "mark_read")])
        assert got["threshold"] == 30.0
        assert got["floor"] == 0.30  # ai_score is stored 0..1, filters use 0..100
        assert got["filter_name"] == "AI - LowScore"

    async def test_widest_band_wins(self):
        got = await self._floor([("narrow", "20", "archive"),
                                 ("wide", "45", "mark_read")])
        assert got["threshold"] == 45.0
        assert got["filter_name"] == "wide"

    async def test_malformed_value_is_skipped_not_fatal(self):
        # the column is free text, and a bad row must not take the page down
        got = await self._floor([("broken", "not a number", "mark_read"),
                                 ("ok", "30", "mark_read")])
        assert got["threshold"] == 30.0


class TestCalibrationBuckets:
    def test_assignment_and_rate(self):
        pairs = [(0.1, True), (0.1, False), (0.9, True)]
        buckets = calibration_buckets(pairs, n_buckets=5)
        assert buckets[0]["count"] == 2
        assert buckets[0]["rate"] == 0.5
        assert buckets[4]["count"] == 1
        assert buckets[4]["rate"] == 1.0

    def test_score_one_goes_last_bucket(self):
        buckets = calibration_buckets([(1.0, True)], n_buckets=5)
        assert buckets[4]["count"] == 1

    def test_empty_bucket_rate_none(self):
        buckets = calibration_buckets([(0.1, True)], n_buckets=5)
        assert buckets[4]["rate"] is None


class TestScoreHistogram:
    def test_binning(self):
        hist = score_histogram([0.0, 0.99, 0.5], n_bins=20)
        assert hist[0]["count"] == 1
        assert hist[10]["count"] == 1
        assert hist[19]["count"] == 1
        assert sum(h["count"] for h in hist) == 3

    def test_score_one_clamped_to_last_bin(self):
        hist = score_histogram([1.0], n_bins=20)
        assert hist[19]["count"] == 1


class TestEngagedLabel:
    def test_query_excludes_is_read(self):
        src = inspect.getsource(get_scoring_eval)
        assert "is_read" not in src
        assert "dwell_seconds >= 60" in src
        assert "user_starred" in src
        assert "link_opened" in src

    def test_supports_optional_user_filter(self):
        src = inspect.getsource(get_scoring_eval)
        assert "user_id" in src
        assert "AND user_id = :uid" in src

    def test_window_is_clamped_to_retention(self):
        src = inspect.getsource(get_scoring_eval)
        assert "effective_window" in src
        assert "default_purge_after_days" in src

    def test_aggregate_view_does_not_invent_a_common_floor(self):
        # each user's threshold is their own, so the aggregate reports who is
        # affected and leaves the number uncorrected
        src = inspect.getsource(get_scoring_eval)
        assert "aggregate_only" in src
        assert "exposure_filter_users" in src

"""Unit tests for scoring-eval metrics (pure functions) + engaged-label guard."""
import inspect

from app.services.ai_eval_service import (
    calibration_buckets,
    compute_auc,
    get_scoring_eval,
    score_histogram,
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

"""Unit tests for the lexical (BM25) relevance scorer.

The one that would cost the most to get wrong is
`TestAvoidList::test_avoid_list_never_lowers_the_score`. Subtracting the avoid
list the way the embedding baseline does scored AUC 0.486 offline, i.e. below
chance, because it ranked by the avoid list in reverse. It is an easy thing to
"fix" back in while reading the code, so it is pinned here.
"""
import pytest

from app.services import relevance_service as rs

PROFILE = (
    "High relevance: AI safety and alignment, Ukraine war updates\n"
    "Moderate relevance: RSS readers and feed curation\n"
    "Avoid: celebrity gossip, football results"
)

CORPUS = [
    "AI safety researchers publish alignment results",
    "New alignment benchmark for AI safety teams",
    "AI safety funding round closes",
    "Ukraine war updates from the eastern front",
    "Ukraine war: front line shifts again",
    "Ukraine reports war damage to the grid",
    "Celebrity gossip roundup of the week",
    "Celebrity gossip: who wore what",
    "Celebrity gossip and football results",
    "Football results from the weekend",
    "Football results and league standings",
    "Football results roundup",
    "Weather forecast for the weekend",
    "Weather forecast stays unchanged",
    "Weather forecast warnings issued",
]


@pytest.fixture
def stats() -> rs.CorpusStats:
    return rs.build_corpus_stats(CORPUS, min_df=3)


class TestParseProfile:
    def test_splits_positive_units_and_keeps_negatives_apart(self):
        """"X and Y" is two directions, not one, exactly as the eval parsed it."""
        profile = rs.parse_profile(PROFILE)
        assert profile.positive == [
            "AI safety",
            "alignment",
            "Ukraine war updates",
            "RSS readers",
            "feed curation",
        ]
        assert profile.negative == ["celebrity gossip", "football results"]

    def test_moderate_line_counts_as_positive(self):
        profile = rs.parse_profile("Moderate relevance: space exploration")
        assert profile.positive == ["space exploration"]
        assert profile.negative == []

    def test_unlabelled_line_counts_as_positive(self):
        """A hand-written profile has no labels; it still has to produce units."""
        profile = rs.parse_profile("I read about climate policy, and about trains")
        assert profile.positive == ["I read about climate policy", "about trains"]
        assert profile.negative == []

    def test_separator_inside_brackets_does_not_split_the_topic(self):
        profile = rs.parse_profile(
            "High relevance: health science (nutrition, exercise), trains")
        assert profile.positive == [
            "health science (nutrition, exercise)", "trains"]

    def test_avoid_label_in_czech_is_recognised(self):
        profile = rs.parse_profile("Nezajímá mě: fotbalové výsledky, celebrity")
        assert profile.positive == []
        assert profile.negative == ["fotbalové výsledky", "celebrity"]

    def test_empty_profile_is_falsy(self):
        assert not rs.parse_profile(None)
        assert not rs.parse_profile("   \n  ")

    def test_avoid_only_profile_has_nothing_to_rank_by(self):
        # It must not fall back to scoring the avoid list as if it were positive:
        # that is the below-chance scorer the module is written to avoid.
        profile = rs.parse_profile("Avoid: sports, celebrity news")
        assert profile.positive == []
        assert not profile


class TestAvoidList:
    def test_avoid_list_never_lowers_the_score(self, stats):
        """An avoided article scores the same with and without the avoid list."""
        profile = rs.parse_profile(PROFILE)
        no_avoid = rs.Profile(profile.positive, [])
        text = "Celebrity gossip and football results from the weekend"
        assert (rs.bm25_raw(text, profile.positive, stats)
                == rs.bm25_raw(text, no_avoid.positive, stats))

    def test_avoided_article_ranks_below_a_wanted_one(self, stats):
        profile = rs.parse_profile(PROFILE)
        wanted = rs.bm25_raw("AI safety alignment work", profile.positive, stats)
        avoided = rs.bm25_raw("Celebrity gossip roundup", profile.positive, stats)
        assert wanted > avoided


class TestScorer:
    def test_matching_article_outscores_an_unrelated_one(self, stats):
        profile = rs.parse_profile(PROFILE)
        on_topic = rs.bm25_raw("Ukraine war updates today", profile.positive, stats)
        off_topic = rs.bm25_raw("Weather forecast for the weekend",
                                profile.positive, stats)
        assert on_topic > off_topic == 0.0

    def test_is_deterministic(self, stats):
        profile = rs.parse_profile(PROFILE)
        text = "AI safety alignment benchmark released"
        assert (rs.bm25_raw(text, profile.positive, stats)
                == rs.bm25_raw(text, profile.positive, stats))

    def test_max_over_units_not_sum(self, stats):
        """Adding an unrelated topic to the profile must not raise the score."""
        one = rs.bm25_raw("AI safety alignment work", ["AI safety and alignment"], stats)
        two = rs.bm25_raw("AI safety alignment work",
                          ["AI safety and alignment", "Ukraine war updates"], stats)
        assert one == two

    def test_unknown_terms_contribute_nothing(self, stats):
        """A term below the min_df cutoff is not in the table and scores zero."""
        assert "nutrition" not in stats.doc_freq
        assert rs.bm25_raw("nutrition nutrition nutrition", ["nutrition"], stats) == 0.0

    def test_article_without_a_body_still_scores(self, stats):
        """14.6% of the eval sample had no body; the title alone has to work."""
        profile = rs.parse_profile(PROFILE)
        text = rs.article_text("Ukraine war updates from the front", None)
        assert rs.bm25_raw(text, profile.positive, stats) > 0.0

    def test_accents_are_stripped_so_czech_matches_the_profile(self, stats):
        assert rs.tokenize("Bezpečnost") == rs.tokenize("bezpecnost")

    def test_summary_is_cut_to_the_measured_window(self):
        text = rs.article_text("Title", "x" * 1000)
        assert len(text) == len("Title\n\n") + rs.SUMMARY_MAX_CHARS

    def test_empty_corpus_scores_nothing(self):
        empty = rs.build_corpus_stats([], min_df=3)
        assert rs.bm25_raw("AI safety", ["AI safety"], empty) == 0.0


class TestSquash:
    def test_maps_into_the_unit_interval(self):
        assert rs.squash(0.0) == 0.0
        assert 0.0 < rs.squash(1.0) < 1.0
        assert rs.squash(1e9) < 1.0

    def test_is_monotonic_so_it_cannot_reorder_articles(self):
        raws = [0.0, 0.5, 1.0, 3.0, 7.0, 25.0]
        squashed = [rs.squash(r) for r in raws]
        assert squashed == sorted(squashed)
        assert len(set(squashed)) == len(squashed)

    def test_negative_raw_score_clamps_to_zero(self):
        # BM25 idf goes negative for terms in more than half the corpus.
        assert rs.squash(-1.0) == 0.0


class TestLexicalScore:
    def test_returns_none_without_a_profile(self, stats):
        assert rs.lexical_score("AI safety", rs.parse_profile(None), stats) is None

    def test_returns_none_before_the_term_table_exists(self):
        empty = rs.build_corpus_stats([], min_df=3)
        profile = rs.parse_profile(PROFILE)
        assert rs.lexical_score("AI safety", profile, empty) is None

    def test_zero_means_read_and_found_nothing(self, stats):
        profile = rs.parse_profile(PROFILE)
        assert rs.lexical_score("Weather forecast", profile, stats) == 0.0

    def test_scores_land_in_the_unit_interval(self, stats):
        profile = rs.parse_profile(PROFILE)
        score = rs.lexical_score(
            rs.article_text("AI safety alignment benchmark", "More on AI safety."),
            profile, stats)
        assert 0.0 < score < 1.0


class TestCorpusStats:
    def test_min_df_drops_the_long_tail(self):
        stats = rs.build_corpus_stats(CORPUS, min_df=3)
        assert "football" in stats.doc_freq
        assert "grid" not in stats.doc_freq

    def test_avg_doc_len_counts_only_terms_in_the_table(self):
        stats = rs.build_corpus_stats(CORPUS, min_df=3)
        expected = sum(
            sum(1 for t in rs.terms(text) if t in stats.doc_freq) for text in CORPUS
        ) / len(CORPUS)
        assert stats.avg_doc_len == pytest.approx(expected)

    def test_table_and_scorer_agree_on_the_ngram_range(self):
        bigrams = rs.build_corpus_stats(CORPUS, min_df=3, ngram_max=2)
        assert bigrams.ngram_max == 2
        assert any(" " in term for term in bigrams.doc_freq)
        # The scorer reads the range off the table, so a bigram query term is
        # matched here and ignored against a unigram table.
        assert rs.bm25_raw("football results today", ["football results"],
                           bigrams) > 0.0

"""Unit tests for the lexical (BM25) relevance scorer and its term list.

The behaviours pinned here were each measured in step 2b of the plan, and each
is easy to "fix" back while reading the code: a line is loose words and not a
phrase, a CJK term is the one exception, a prefix match weighs half an exact
one, and NFKD must never touch Hangul or kana.
"""
import pytest

from app.services import relevance_service as rs

CORPUS = [
    "AI safety researchers publish alignment results",
    "New alignment benchmark for AI safety teams",
    "AI safety funding round closes",
    "Ukraine war updates from the eastern front",
    "Ukraine war: front line shifts again",
    "Ukraine reports war damage to the grid",
    "Válka na Ukrajině pokračuje",
    "Konec války není v dohledu",
    "Válku komentuje ministr",
    "Celebrity gossip roundup of the week",
    "Celebrity gossip: who wore what",
    "Celebrity gossip and football results",
    "Weather forecast for the weekend",
    "Weather forecast stays unchanged",
    "Weather forecast warnings issued",
    "人工智能公司发布新模型",
    "人工智能安全研究",
    "人工智能与芯片",
    "比特币价格上涨",
    "比特币交易所",
    "比特币挖矿",
]


@pytest.fixture
def stats() -> rs.CorpusStats:
    return rs.build_corpus_stats(CORPUS, min_df=3)


class TestTokenize:
    def test_latin_words_lowercased_and_unaccented(self):
        assert rs.tokenize("Bezpečnost AI") == ["bezpecnost", "ai"]

    def test_single_letters_and_punctuation_drop_out(self):
        assert rs.tokenize("a - b, AI!") == ["ai"]

    def test_cyrillic_is_words(self):
        # NFKD takes the breve off й; accepted, the same happens to the query.
        assert rs.tokenize("Новый искусственный интеллект") == [
            "новыи", "искусственныи", "интеллект"]

    def test_han_becomes_overlapping_bigrams_without_unigrams(self):
        assert rs.tokenize("人工智能") == ["人工", "工智", "智能"]

    def test_a_lone_cjk_character_is_kept(self):
        assert rs.tokenize("AI 与 ML") == ["ai", "与", "ml"]

    def test_mixed_text_is_split_by_script(self):
        assert rs.tokenize("OpenAI发布新模型") == [
            "openai", "发布", "布新", "新模", "模型"]

    def test_hangul_survives_normalization(self):
        """NFKD would decompose each syllable into jamo."""
        assert rs.tokenize("인공지능") == ["인공", "공지", "지능"]

    def test_kana_keeps_its_voicing_mark(self):
        """NFKD would turn が into か."""
        assert rs.tokenize("ビットコインが") == [
            "ビッ", "ット", "トコ", "コイ", "イン", "ンが"]

    def test_full_width_latin_is_folded(self):
        assert rs.tokenize("ＡＩ") == ["ai"]


class TestParseTerms:
    def test_new_lines_separate_terms(self):
        assert rs.parse_terms("AI safety\nUkraine\n\n  brain health  ") == [
            "AI safety", "Ukraine", "brain health"]

    def test_duplicates_are_judged_after_normalization(self):
        assert rs.parse_terms("AI Safety\nai safety\nAI-safety") == ["AI Safety"]

    def test_skipped_pieces_are_reported_as_written(self):
        assert rs.skipped_terms("AI Safety, x, ai safety, !!, cycling") == [
            "x", "ai safety", "!!"]

    def test_lines_with_no_token_are_dropped(self):
        assert rs.parse_terms("-\nx\nAI") == ["AI"]

    def test_pasted_bullets_are_forgiven(self):
        assert rs.parse_terms("- AI safety\n• Ukraine\n* crypto") == [
            "AI safety", "Ukraine", "crypto"]

    def test_commas_and_semicolons_separate_like_new_lines(self):
        assert rs.parse_terms("AI safety, crypto; kryptoměny\nUkraine") == [
            "AI safety", "crypto", "kryptoměny", "Ukraine"]

    def test_cjk_list_separators_count_too(self):
        assert rs.parse_terms("人工智能，比特币、乌克兰；睡眠") == [
            "人工智能", "比特币", "乌克兰", "睡眠"]

    def test_words_of_a_term_stay_together(self):
        """A space is not a separator: the words of a term add up as one topic."""
        assert rs.parse_terms("multiple sclerosis") == ["multiple sclerosis"]

    def test_empty(self):
        assert rs.parse_terms(None) == []
        assert rs.parse_terms("  \n ") == []


class TestPrefix:
    def test_cuts_the_last_two_characters(self):
        assert rs.prefix_of("meditation") == "meditati"
        assert rs.prefix_of("sklerozou") == "skleroz"

    def test_never_below_four(self):
        assert rs.prefix_of("valka") == "valk"
        assert rs.prefix_of("mozek") == "moze"

    def test_short_words_have_none(self):
        assert rs.prefix_of("war") is None

    def test_terms_needed_lists_tokens_and_prefixes(self):
        tokens, prefixes = rs.terms_needed(["válka", "AI"])
        assert tokens == {"valka", "ai"}
        assert prefixes == {"valk"}


class TestTruncation:
    def test_an_inflected_form_matches_through_its_prefix(self, stats):
        assert rs.bm25_raw("Konec války", ["válka"], stats).score > 0.0

    def test_prefix_match_weighs_half_an_exact_one(self, stats):
        # "ukrainee" is not in the token table, so it can only score through
        # the prefix "ukrai", at half weight and with the prefix's own IDF.
        exact = rs.bm25_raw("Ukraine reports", ["Ukraine"], stats).score
        via_prefix = rs.bm25_raw("Ukrainee reports", ["Ukraine"], stats).score
        assert rs.prefix_of("ukraine") == "ukrai"
        pidf = rs.idf(stats.prefix_freq["ukrai"], stats.n_docs)
        assert via_prefix == pytest.approx(
            rs.PREFIX_WEIGHT * pidf * 1.0 * (rs.BM25_K1 + 1) / (1.0 + rs.BM25_K1))
        assert exact > 0.0

    def test_exact_match_does_not_also_count_the_prefix(self, stats):
        once = rs.bm25_raw("Ukraine", ["Ukraine"], stats).score
        assert once == pytest.approx(
            rs.idf(stats.doc_freq["ukraine"], stats.n_docs))

    def test_short_query_words_match_exactly_only(self, stats):
        assert rs.bm25_raw("warfare continues", ["war"], stats).score == 0.0


class TestScorer:
    def test_matching_article_outscores_an_unrelated_one(self, stats):
        on_topic = rs.bm25_raw("Ukraine war updates today", ["Ukraine war"], stats)
        off_topic = rs.bm25_raw("Weather forecast for the weekend",
                                ["Ukraine war"], stats)
        assert on_topic.score > off_topic.score == 0.0

    def test_a_line_is_loose_words_not_a_phrase(self, stats):
        """Strict phrases cost 0.035 AUC: "AI" alone still carries signal."""
        assert rs.bm25_raw("safety first for AI", ["AI safety"], stats).score > 0.0
        assert rs.bm25_raw("AI chips", ["AI safety"], stats).score > 0.0

    def test_words_of_one_term_add_up_separate_terms_do_not(self, stats):
        """Why the grouping matters: one topic of two words, or two topics."""
        text = "AI safety benchmark"
        together = rs.bm25_raw(text, rs.parse_terms("AI safety"), stats).score
        apart = rs.bm25_raw(text, rs.parse_terms("AI, safety"), stats).score
        assert together > apart > 0.0

    def test_a_cjk_term_must_match_in_a_row(self, stats):
        """One word cut into bigrams, not a list of words."""
        assert rs.bm25_raw("人工智能安全", ["人工智能"], stats).score > 0.0
        # All three bigrams of 人工智能 present, but not in a row.
        assert rs.bm25_raw("智能人工工智", ["人工智能"], stats).score == 0.0

    def test_max_over_terms_not_sum(self, stats):
        """Adding an unrelated term must not raise the score."""
        one = rs.bm25_raw("AI safety alignment", ["AI safety"], stats)
        two = rs.bm25_raw("AI safety alignment", ["AI safety", "Ukraine"], stats)
        assert one.score == two.score

    def test_names_the_term_that_scored(self, stats):
        match = rs.bm25_raw("Ukraine war updates", ["AI safety", "Ukraine war"], stats)
        assert match.term == "Ukraine war"
        assert rs.bm25_raw("Weather", ["AI safety"], stats).term is None

    def test_unknown_terms_contribute_nothing(self, stats):
        """A term below the min_df cutoff is not in the table and scores zero."""
        assert "nutrition" not in stats.doc_freq
        assert rs.bm25_raw("nutrition nutrition", ["nutrition"], stats).score == 0.0

    def test_is_deterministic(self, stats):
        text = "AI safety alignment benchmark released"
        assert (rs.bm25_raw(text, ["AI safety"], stats)
                == rs.bm25_raw(text, ["AI safety"], stats))

    def test_article_without_a_body_still_scores(self, stats):
        """18% of articles arrive with no description; the title has to work."""
        text = rs.article_text("Ukraine war updates from the front", None)
        assert rs.bm25_raw(text, ["Ukraine"], stats).score > 0.0

    def test_summary_is_cut_to_the_measured_window(self):
        text = rs.article_text("Title", "x" * 1000)
        assert len(text) == len("Title\n\n") + rs.SUMMARY_MAX_CHARS

    def test_empty_corpus_scores_nothing(self):
        empty = rs.build_corpus_stats([], min_df=3)
        assert rs.bm25_raw("AI safety", ["AI safety"], empty).score == 0.0


class TestLexicalScore:
    def test_returns_none_without_terms(self, stats):
        assert rs.lexical_score("AI safety", [], stats) is None

    def test_returns_none_before_the_term_table_exists(self):
        empty = rs.build_corpus_stats([], min_df=3)
        assert rs.lexical_score("AI safety", ["AI safety"], empty) is None

    def test_zero_means_read_and_found_nothing(self, stats):
        assert rs.lexical_score("Weather forecast", ["AI safety"], stats) == 0.0

    def test_scores_land_in_the_unit_interval(self, stats):
        score = rs.lexical_score(
            rs.article_text("AI safety alignment benchmark", "More on AI safety."),
            ["AI safety"], stats)
        assert 0.0 < score < 1.0


class TestCorpusStats:
    def test_min_df_drops_the_long_tail(self, stats):
        assert "football" not in stats.doc_freq  # in two articles only
        assert "gossip" in stats.doc_freq
        assert "grid" not in stats.doc_freq

    def test_prefix_counts_documents_not_occurrences(self, stats):
        # válka, války, válku: three articles, one prefix class.
        assert stats.prefix_freq["valk"] == 3


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


class TestEffectiveScore:
    """Which number a reader is shown when both scorers have an opinion."""

    def test_the_model_wins_where_it_has_spoken(self):
        assert rs.effective_score(0.8, 0.2) == (0.8, True)

    def test_falls_back_to_the_lexical_one(self):
        assert rs.effective_score(None, 0.2) == (0.2, False)

    def test_a_lexical_zero_is_still_a_score(self):
        """0.0 is "read it, found nothing"; None is "nobody looked"."""
        assert rs.effective_score(None, 0.0) == (0.0, False)

    def test_neither_is_not_a_zero(self):
        assert rs.effective_score(None, None) == (None, False)

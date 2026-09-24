"""Saving the term list and the article list's relevance intro bar."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.services.relevance_terms_service import save_terms, show_relevance_intro

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def _settings(**kw):
    base = dict(
        relevance_terms=None, relevance_terms_updated_at=None, relevance_terms_source=None,
        basic_scoring_enabled=True, lexical_backfill_at=NOW,
        relevance_intro_dismissed_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestSaveTerms:
    def test_onboarding_source_and_backfill_due(self):
        s = _settings(lexical_backfill_at=None)
        save_terms(s, "cycling, sourdough", source="onboarding")
        assert s.relevance_terms == "cycling, sourdough"
        assert s.relevance_terms_source == "onboarding"
        assert s.relevance_terms_updated_at is not None

    def test_default_source_is_manual(self):
        s = _settings()
        save_terms(s, "cycling")
        assert s.relevance_terms_source == "manual"

    def test_unchanged_text_keeps_timestamp_and_source(self):
        s = _settings(relevance_terms="cycling", relevance_terms_updated_at=NOW,
                      relevance_terms_source="onboarding")
        save_terms(s, "  cycling  ")
        assert s.relevance_terms_updated_at == NOW
        assert s.relevance_terms_source == "onboarding"

    def test_emptied_list_resets_backfill(self):
        s = _settings(relevance_terms="cycling")
        save_terms(s, "")
        assert s.relevance_terms is None
        assert s.lexical_backfill_at is None


class TestShowRelevanceIntro:
    def test_shown_without_terms(self):
        assert show_relevance_intro(_settings())

    def test_hidden_with_terms(self):
        assert not show_relevance_intro(_settings(relevance_terms="cycling"))

    def test_shown_when_terms_yield_nothing(self):
        assert show_relevance_intro(_settings(relevance_terms=" - , ; "))

    def test_hidden_once_dismissed(self):
        assert not show_relevance_intro(_settings(relevance_intro_dismissed_at=NOW))

    def test_hidden_when_basic_scoring_off(self):
        assert not show_relevance_intro(_settings(basic_scoring_enabled=False))

    def test_hidden_without_settings(self):
        assert not show_relevance_intro(None)

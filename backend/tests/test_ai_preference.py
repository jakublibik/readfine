"""Unit tests for interest-profile prompt assembly (pure functions)."""
from app.services.ai_service import (
    _PREF_SECTIONS,
    _build_preference_prompt,
    _pref_snippet,
)


class TestPrefSnippet:
    def test_prefers_ai_summary(self):
        assert _pref_snippet("summary", "readable", "content") == "summary"

    def test_falls_back_to_readable(self):
        assert _pref_snippet(None, "readable", "content") == "readable"

    def test_falls_back_to_content(self):
        assert _pref_snippet(None, None, "content") == "content"

    def test_all_none_returns_empty(self):
        assert _pref_snippet(None, None, None) == ""

    def test_strips_html_and_normalizes_whitespace(self):
        assert _pref_snippet(None, "<p>hello   world</p>", None) == "hello world"

    def test_truncates_at_default_300(self):
        assert len(_pref_snippet(None, None, "x" * 500)) == 300

    def test_custom_limit(self):
        assert len(_pref_snippet(None, None, "x" * 500, limit=10)) == 10


class TestBuildPreferencePrompt:
    def _groups(self):
        return {
            "g1": [("AI title", "ai snippet")],
            "g2": [("Read title", "read snippet")],
        }

    def test_includes_headers_for_nonempty_groups(self):
        prompt = _build_preference_prompt(self._groups(), "")
        assert _PREF_SECTIONS["g1"] in prompt
        assert _PREF_SECTIONS["g2"] in prompt

    def test_includes_title_and_snippet(self):
        prompt = _build_preference_prompt(self._groups(), "")
        assert "- AI title — ai snippet" in prompt

    def test_omits_empty_groups(self):
        prompt = _build_preference_prompt(self._groups(), "")
        assert _PREF_SECTIONS["p1"] not in prompt
        assert _PREF_SECTIONS["n1"] not in prompt

    def test_p1_n1_present_when_provided(self):
        groups = self._groups()
        groups["p1"] = [("low score title", "snip")]
        groups["n1"] = [("high score title", "snip")]
        prompt = _build_preference_prompt(groups, "")
        assert _PREF_SECTIONS["p1"] in prompt
        assert _PREF_SECTIONS["n1"] in prompt

    def test_includes_recurring_and_narrow_rules(self):
        prompt = _build_preference_prompt(self._groups(), "")
        assert "RECURRING" in prompt
        assert "Do NOT move them to Avoid" in prompt

    def test_three_line_output_format(self):
        prompt = _build_preference_prompt(self._groups(), "")
        assert "High relevance:" in prompt
        assert "Moderate relevance:" in prompt
        assert "Avoid:" in prompt

    def test_empty_groups_fallback(self):
        prompt = _build_preference_prompt({}, "")
        assert "(no reading history yet)" in prompt

    def test_feeds_str_included(self):
        prompt = _build_preference_prompt(
            {}, "Subscribed feeds (general context):\n- Feed A\n\n"
        )
        assert "Feed A" in prompt

    def test_title_without_snippet_has_no_dash(self):
        prompt = _build_preference_prompt({"g1": [("Title only", "")]}, "")
        assert "- Title only" in prompt
        assert "Title only — " not in prompt

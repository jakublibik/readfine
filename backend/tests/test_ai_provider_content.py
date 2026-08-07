"""Unit tests for provider response text extraction.

Blocked/empty/filtered provider responses must raise ProviderEmptyResponse
(a controlled error callers already handle) rather than crashing on .strip().

Also covers the stop-reason read that tells a complete summary from one the model
cut off on its token cap, and the cap itself.
"""
import pytest
from types import SimpleNamespace

from app.services.ai_service import (
    _extract_text,
    _extract_truncated,
    _openai_max_tokens,
    _summary_token_budget,
    _OPENAI_REASONING_BUDGET,
    _SUMMARY_CHARS_PER_TOKEN,
    _SUMMARY_MAX_TOKENS,
    _SUMMARY_MIN_TOKENS,
    ProviderEmptyResponse,
)


def _anthropic(text, *, leading_thinking=False):
    """An Anthropic response. Real content blocks always carry ``type``, and the
    extraction keys off it, so the stub has to as well."""
    blocks = [] if text is None else [SimpleNamespace(type="text", text=text)]
    if leading_thinking:
        blocks.insert(0, SimpleNamespace(type="thinking", thinking="deliberating"))
    return SimpleNamespace(content=blocks)


def _openai(content, finish_reason="stop"):
    """finish_reason is required on a real Choice, so the stub always carries one."""
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=content),
    )])


def _gemini(text):
    return SimpleNamespace(text=text)


class TestExtractTextSuccess:
    def test_anthropic(self):
        assert _extract_text("anthropic", _anthropic("  hello  ")) == "hello"

    def test_anthropic_skips_a_leading_thinking_block(self):
        """Models that think by default open with a thinking block; the text one
        behind it is the answer, and reading blocks[0] would miss it."""
        resp = _anthropic("the answer", leading_thinking=True)
        assert _extract_text("anthropic", resp) == "the answer"

    def test_openai(self):
        assert _extract_text("openai", _openai("hi there")) == "hi there"

    def test_gemini(self):
        assert _extract_text("gemini", _gemini("world\n")) == "world"


class TestExtractTextEmpty:
    @pytest.mark.parametrize("resp", [_anthropic(None), _anthropic(""), _anthropic("   ")])
    def test_anthropic_empty_raises(self, resp):
        with pytest.raises(ProviderEmptyResponse):
            _extract_text("anthropic", resp)

    def test_anthropic_no_blocks_raises(self):
        with pytest.raises(ProviderEmptyResponse):
            _extract_text("anthropic", SimpleNamespace(content=[]))

    @pytest.mark.parametrize("content", [None, "", "  "])
    def test_openai_empty_raises(self, content):
        with pytest.raises(ProviderEmptyResponse):
            _extract_text("openai", _openai(content))

    def test_openai_no_choices_raises(self):
        with pytest.raises(ProviderEmptyResponse):
            _extract_text("openai", SimpleNamespace(choices=[]))

    @pytest.mark.parametrize("text", [None, "", "  "])
    def test_gemini_empty_raises(self, text):
        with pytest.raises(ProviderEmptyResponse):
            _extract_text("gemini", _gemini(text))


class TestEmptyResponseDetail:
    """The message is what lands in the user's error banner, so it has to name the
    reason: a refusal, a hit token cap and a genuinely empty reply need different
    responses from whoever reads it."""

    def test_anthropic_names_stop_reason_and_block_types(self):
        resp = SimpleNamespace(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="thinking", thinking="")],
        )
        with pytest.raises(ProviderEmptyResponse) as exc:
            _extract_text("anthropic", resp)
        assert "stop_reason=max_tokens" in str(exc.value)
        assert "blocks=[thinking]" in str(exc.value)

    def test_anthropic_refusal_is_distinguishable(self):
        resp = SimpleNamespace(stop_reason="refusal", content=[])
        with pytest.raises(ProviderEmptyResponse) as exc:
            _extract_text("anthropic", resp)
        assert "stop_reason=refusal" in str(exc.value)
        assert "blocks=[]" in str(exc.value)

    def test_openai_names_finish_reason(self):
        resp = SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="content_filter",
            message=SimpleNamespace(content=None),
        )])
        with pytest.raises(ProviderEmptyResponse) as exc:
            _extract_text("openai", resp)
        assert "finish_reason=content_filter" in str(exc.value)

    def test_gemini_names_block_reason_when_the_prompt_was_refused(self):
        resp = SimpleNamespace(
            text=None,
            candidates=[],
            prompt_feedback=SimpleNamespace(block_reason=SimpleNamespace(name="SAFETY")),
        )
        with pytest.raises(ProviderEmptyResponse) as exc:
            _extract_text("gemini", resp)
        assert "block_reason=SAFETY" in str(exc.value)

    def test_broken_diagnostics_still_raise_the_real_error(self):
        """A provider reshaping a field must not turn the controlled error into an
        AttributeError from the diagnostics path."""
        class Hostile:
            content = []
            @property
            def stop_reason(self):
                raise RuntimeError("shape changed")

        with pytest.raises(ProviderEmptyResponse) as exc:
            _extract_text("anthropic", Hostile())
        assert "returned no usable content" in str(exc.value)


def test_unknown_provider_raises_value_error():
    with pytest.raises(ValueError):
        _extract_text("nope", SimpleNamespace())


class TestOpenAIMaxTokens:
    @pytest.mark.parametrize("model", [
        "o1", "o1-mini", "o3-mini", "o4-mini",
        "gpt-5", "GPT-5-mini", "gpt-5.4", "gpt-5.5", "gpt-5.1-mini",
    ])
    def test_reasoning_models_get_headroom(self, model):
        assert _openai_max_tokens(model, 10) == 10 + _OPENAI_REASONING_BUDGET

    @pytest.mark.parametrize("model", ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"])
    def test_non_reasoning_models_unchanged(self, model):
        assert _openai_max_tokens(model, 500) == 500

    def test_none_model_unchanged(self):
        assert _openai_max_tokens(None, 500) == 500


class TestExtractTruncated:
    """A summary that stopped on the token cap must be recognisable, and anything
    unexpected must read as "not truncated" so a provider quirk cannot turn a good
    completion into a flagged one."""

    @pytest.mark.parametrize("stop_reason, expected", [
        ("max_tokens", True),
        ("end_turn", False),
        (None, False),
    ])
    def test_anthropic(self, stop_reason, expected):
        resp = SimpleNamespace(stop_reason=stop_reason)
        assert _extract_truncated("anthropic", resp) is expected

    @pytest.mark.parametrize("finish_reason, expected", [
        ("length", True),
        ("stop", False),
        (None, False),
    ])
    def test_openai(self, finish_reason, expected):
        resp = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason)])
        assert _extract_truncated("openai", resp) is expected

    def test_openai_no_choices(self):
        assert _extract_truncated("openai", SimpleNamespace(choices=[])) is False

    @pytest.mark.parametrize("reason_name, expected", [
        ("MAX_TOKENS", True),
        ("STOP", False),
    ])
    def test_gemini_enum_reason(self, reason_name, expected):
        # google-genai hands back an enum; only its .name is relied on.
        resp = SimpleNamespace(candidates=[
            SimpleNamespace(finish_reason=SimpleNamespace(name=reason_name))
        ])
        assert _extract_truncated("gemini", resp) is expected

    def test_gemini_no_candidates(self):
        assert _extract_truncated("gemini", SimpleNamespace(candidates=[])) is False

    def test_missing_attributes_read_as_not_truncated(self):
        for provider in ("anthropic", "openai", "gemini"):
            assert _extract_truncated(provider, SimpleNamespace()) is False

    def test_unknown_provider_reads_as_not_truncated(self):
        assert _extract_truncated("nope", SimpleNamespace()) is False


class TestSummaryTokenBudget:
    """The summary prompt scales length with the article, so the output cap does
    too — but never below a usable floor or above the cost ceiling."""

    def test_short_article_gets_the_floor(self):
        assert _summary_token_budget("x" * 100) == _SUMMARY_MIN_TOKENS

    def test_long_article_capped_at_ceiling(self):
        assert _summary_token_budget("x" * 10_000_000) == _SUMMARY_MAX_TOKENS

    def test_scales_between_floor_and_ceiling(self):
        chars = (_SUMMARY_MIN_TOKENS + _SUMMARY_MAX_TOKENS) // 2 * _SUMMARY_CHARS_PER_TOKEN
        budget = _summary_token_budget("x" * chars)
        assert _SUMMARY_MIN_TOKENS < budget < _SUMMARY_MAX_TOKENS
        assert budget == chars // _SUMMARY_CHARS_PER_TOKEN

    def test_longer_article_never_gets_a_smaller_budget(self):
        lengths = [0, 5_000, 20_000, 50_000, 200_000]
        budgets = [_summary_token_budget("x" * n) for n in lengths]
        assert budgets == sorted(budgets)

    def test_empty_content_still_gets_the_floor(self):
        assert _summary_token_budget("") == _SUMMARY_MIN_TOKENS

"""Unit tests for provider response text extraction.

Blocked/empty/filtered provider responses must raise ProviderEmptyResponse
(a controlled error callers already handle) rather than crashing on .strip().
"""
import pytest
from types import SimpleNamespace

from app.services.ai_service import (
    _extract_text,
    _openai_max_tokens,
    _OPENAI_REASONING_BUDGET,
    ProviderEmptyResponse,
)


def _anthropic(text):
    block = SimpleNamespace(text=text)
    return SimpleNamespace(content=[block] if text is not None else [])


def _openai(content):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def _gemini(text):
    return SimpleNamespace(text=text)


class TestExtractTextSuccess:
    def test_anthropic(self):
        assert _extract_text("anthropic", _anthropic("  hello  ")) == "hello"

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

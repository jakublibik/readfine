"""Unit tests for provider response text extraction.

Blocked/empty/filtered provider responses must raise ProviderEmptyResponse
(a controlled error callers already handle) rather than crashing on .strip().

Also covers the stop-reason read that tells a complete summary from one the model
cut off on its token cap, the cap itself, and the request-side counterpart: asking
Anthropic to leave thinking out of that cap.
"""
import asyncio

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import ai_service
from app.services.ai_service import (
    _anthropic_create,
    _empty_response_detail,
    _extract_text,
    _extract_truncated,
    _openai_max_tokens,
    _rejects_thinking_param,
    _summary_token_budget,
    _ANTHROPIC_REASONING_BUDGET,
    _ANTHROPIC_THINKING_OFF,
    _OPENAI_REASONING_BUDGET,
    _SUMMARY_CHARS_PER_TOKEN,
    _SUMMARY_MAX_TOKENS,
    _SUMMARY_MIN_TOKENS,
    ModelCannotSkipThinking,
    ProviderEmptyResponse,
)


class _ApiError(Exception):
    """Stands in for an SDK error: the HTTP status is both an attribute and part
    of the message, which is the shape _friendly_ai_error reads."""

    def __init__(self, status_code, message):
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code


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


class TestRejectsThinkingParam:
    """Only a 400 about the parameter itself may trigger the retry — anything else
    has to keep surfacing as the error it is."""

    def test_400_naming_thinking(self):
        exc = _ApiError(400, "thinking.type: Input should be 'adaptive'")
        assert _rejects_thinking_param(exc) is True

    def test_400_naming_thinking_in_any_case(self):
        assert _rejects_thinking_param(_ApiError(400, "Thinking cannot be disabled")) is True

    def test_400_about_something_else(self):
        assert _rejects_thinking_param(_ApiError(400, "max_tokens must be >= 1")) is False

    @pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
    def test_other_statuses_never_retry(self, status):
        """A bad key or a wrong model name mentioning thinking must not be swallowed."""
        assert _rejects_thinking_param(_ApiError(status, "thinking")) is False

    def test_error_without_a_status_code(self):
        assert _rejects_thinking_param(RuntimeError("thinking")) is False


class TestAnthropicCreate:
    """Newer Anthropic models think unasked and spend max_tokens doing it, so every
    request says thinking is off. Models that refuse to turn it off must still work."""

    @pytest.mark.asyncio
    async def test_asks_for_thinking_off(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value="ok")))
        result = await _anthropic_create(client, model="claude-sonnet-5", max_tokens=10)
        assert result == "ok"
        kwargs = client.messages.create.call_args.kwargs
        assert kwargs["thinking"] == _ANTHROPIC_THINKING_OFF
        assert kwargs["max_tokens"] == 10
        assert kwargs["model"] == "claude-sonnet-5"

    @pytest.mark.asyncio
    async def test_model_that_refuses_gets_the_plain_request(self):
        create = AsyncMock(side_effect=[
            _ApiError(400, "thinking: 'disabled' is not supported by this model"),
            "ok",
        ])
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        result = await _anthropic_create(client, model="claude-fable-5", max_tokens=10)
        assert result == "ok"
        assert create.call_count == 2
        assert "thinking" not in create.call_args.kwargs
        # The retry is the request we would have sent before any of this existed.
        assert create.call_args.kwargs == {"model": "claude-fable-5", "max_tokens": 10}

    @pytest.mark.asyncio
    async def test_unrelated_400_is_not_retried(self):
        create = AsyncMock(side_effect=_ApiError(400, "credit balance is too low"))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with pytest.raises(_ApiError):
            await _anthropic_create(client, model="claude-sonnet-5", max_tokens=10)
        assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_auth_error_is_not_retried(self):
        create = AsyncMock(side_effect=_ApiError(401, "invalid x-api-key"))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with pytest.raises(_ApiError):
            await _anthropic_create(client, model="claude-sonnet-5", max_tokens=10)
        assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_headroom_lets_the_answer_survive_alongside_reasoning(self):
        """A model that cannot switch thinking off shares max_tokens with it, so a
        summary sized for the answer alone came back cut off mid-sentence."""
        create = AsyncMock(side_effect=[_ApiError(400, "thinking cannot be disabled"), "ok"])
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        result = await _anthropic_create(
            client, reasoning_headroom=8000, model="claude-fable-5", max_tokens=400,
        )
        assert result == "ok"
        assert create.call_args.kwargs["max_tokens"] == 8400

    @pytest.mark.asyncio
    async def test_headroom_is_not_spent_when_the_model_took_the_first_request(self):
        """A model that switched thinking off has nothing to make room for."""
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value="ok")))
        await _anthropic_create(
            client, reasoning_headroom=8000, model="claude-sonnet-5", max_tokens=400,
        )
        assert client.messages.create.call_args.kwargs["max_tokens"] == 400

    @pytest.mark.asyncio
    async def test_a_tight_budget_asks_for_no_headroom(self):
        """Scoring wants one decimal in 10 tokens. An always-thinking model cannot
        do that at any ceiling worth paying for, so it fails rather than costing
        8000 tokens per article."""
        create = AsyncMock(side_effect=[_ApiError(400, "thinking cannot be disabled"), "ok"])
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        await _anthropic_create(client, model="claude-fable-5", max_tokens=10)
        assert create.call_args.kwargs["max_tokens"] == 10

    @pytest.mark.asyncio
    async def test_a_refusing_model_that_fails_again_raises_the_second_error(self):
        create = AsyncMock(side_effect=[
            _ApiError(400, "thinking cannot be disabled"),
            _ApiError(429, "rate limit"),
        ])
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with pytest.raises(_ApiError) as exc:
            await _anthropic_create(client, model="claude-fable-5", max_tokens=10)
        assert exc.value.status_code == 429


class TestEmptyResponseDetailNamesCrowdedOutAnswer:
    """When reasoning we could not switch off eats the budget before the answer
    begins, the banner has to say the model is wrong for the slot rather than leave
    it looking like a broken key."""

    def _detail(self, stop_reason, block_types):
        return _empty_response_detail("anthropic", SimpleNamespace(
            stop_reason=stop_reason,
            content=[SimpleNamespace(type=t) for t in block_types],
        ))

    def test_thinking_block_on_a_hit_cap_is_explained(self):
        detail = self._detail("max_tokens", ["thinking"])
        assert "reasoning" in detail
        assert "not suited to this slot" in detail
        # The raw signature stays — it is what a bug report needs.
        assert "stop_reason=max_tokens" in detail

    def test_a_hit_cap_without_thinking_is_left_alone(self):
        """An ordinary model that simply ran out of room is a different problem."""
        assert "reasoning" not in self._detail("max_tokens", ["text"])

    def test_a_refusal_carrying_thinking_is_left_alone(self):
        """Stopped for its own reasons, not crowded out."""
        assert "reasoning" not in self._detail("refusal", ["thinking"])

    def test_keyed_on_the_response_not_a_model_name(self):
        """No model list to keep up to date: any model showing this signature is
        described the same way."""
        assert "not suited to this slot" in self._detail("max_tokens", ["thinking", "text"])


class TestVerifyAiSlot:
    """The connection test has to fail on exactly what a real call would fail on.
    A model that accepts the request and writes nothing back used to pass it, so
    Settings → AI said the slot was fine while every summary and score errored."""

    @staticmethod
    def _verify(monkeypatch, create):
        client = SimpleNamespace(messages=SimpleNamespace(create=create))

        async def fake_get_ai_client(user_id, slot, db):
            return client, "anthropic", "claude-sonnet-5"

        monkeypatch.setattr(ai_service, "get_ai_client", fake_get_ai_client)
        return ai_service.verify_ai_slot(1, "fast", db=None)

    @pytest.mark.asyncio
    async def test_a_real_answer_passes(self, monkeypatch):
        resp = _anthropic("Hello!")
        result = await self._verify(monkeypatch, AsyncMock(return_value=resp))
        assert result == {"ok": True, "model": "claude-sonnet-5", "error": None}

    @pytest.mark.asyncio
    async def test_thinking_only_response_fails_with_the_reason(self, monkeypatch):
        """What a thinking-by-default model returns when the budget went to thinking."""
        resp = SimpleNamespace(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="thinking", thinking="")],
        )
        result = await self._verify(monkeypatch, AsyncMock(return_value=resp))
        assert result["ok"] is False
        assert "stop_reason=max_tokens" in result["error"]

    @pytest.mark.asyncio
    async def test_api_errors_still_come_back_friendly(self, monkeypatch):
        create = AsyncMock(side_effect=_ApiError(401, "invalid x-api-key"))
        result = await self._verify(monkeypatch, create)
        assert result == {"ok": False, "model": "claude-sonnet-5",
                          "error": "Invalid API key."}

    @pytest.mark.asyncio
    async def test_no_client_configured(self, monkeypatch):
        async def fake_get_ai_client(user_id, slot, db):
            return None, None, None

        monkeypatch.setattr(ai_service, "get_ai_client", fake_get_ai_client)
        result = await ai_service.verify_ai_slot(1, "fast", db=None)
        assert result["ok"] is False
        assert result["model"] is None


class TestScoringRefusesAnAlwaysThinkingModel:
    """Scoring answers in ten tokens, which a model that reasons unasked spends
    before it writes anything. Retrying that request without the thinking parameter
    only buys the same empty answer, so the request is not sent: the model is turned
    down where it is chosen, and the job it would have failed gives up at once."""

    @pytest.mark.asyncio
    async def test_the_pointless_retry_is_not_sent(self):
        create = AsyncMock(side_effect=_ApiError(400, "thinking cannot be disabled"))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with pytest.raises(ModelCannotSkipThinking) as exc:
            await _anthropic_create(
                client, require_thinking_off=True,
                model="claude-fable-5", max_tokens=10,
            )
        assert create.call_count == 1
        assert "claude-fable-5" in str(exc.value)

    @pytest.mark.asyncio
    async def test_a_model_that_accepts_the_parameter_is_untouched(self):
        client = SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value="ok")))
        result = await _anthropic_create(
            client, require_thinking_off=True, model="claude-haiku-4-5", max_tokens=10,
        )
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_an_unrelated_400_stays_itself(self):
        """Only a refusal of the parameter means the model is wrong for the slot."""
        create = AsyncMock(side_effect=_ApiError(400, "credit balance is too low"))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with pytest.raises(_ApiError):
            await _anthropic_create(
                client, require_thinking_off=True, model="claude-haiku-4-5", max_tokens=10,
            )

    @pytest.mark.asyncio
    async def test_scoring_is_the_caller_that_asks_for_it(self):
        create = AsyncMock(side_effect=_ApiError(400, "thinking cannot be disabled"))
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        with pytest.raises(ModelCannotSkipThinking):
            await ai_service.score_article(
                "An article.", "Likes tests", client, "anthropic", "claude-fable-5",
            )
        assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_summaries_keep_their_retry(self):
        """The same model is fine for a summary, which can be given room to think."""
        create = AsyncMock(side_effect=[
            _ApiError(400, "thinking cannot be disabled"),
            SimpleNamespace(
                content=[SimpleNamespace(type="text", text="A summary.")],
                usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                stop_reason="end_turn",
            ),
        ])
        client = SimpleNamespace(messages=SimpleNamespace(create=create))
        answer = await ai_service.summarize_article(
            "An article.", client, "anthropic", "claude-fable-5",
        )
        assert answer.text == "A summary."
        assert create.call_count == 2

    def test_the_job_retry_policy_reads_it_as_permanent(self):
        """Three attempts at a request that cannot succeed is three times the cost
        and two hours of backoff before the banner says anything."""
        from app.services.ai_jobs import extract_http_status

        assert extract_http_status(ModelCannotSkipThinking("claude-fable-5")) == 400


class TestScoringModelRejection:
    """What the settings form asks before storing a fast model. Only the model
    answering for itself may block a save; every other failure leaves the choice
    to the person making it."""

    @staticmethod
    def _probe(monkeypatch, create, key="sk-test"):
        async def fake_get_api_key(user_id, provider, db):
            return key

        monkeypatch.setattr(ai_service, "get_api_key", fake_get_api_key)
        monkeypatch.setattr(
            ai_service, "_make_client",
            lambda provider, api_key: SimpleNamespace(
                messages=SimpleNamespace(create=create)),
        )
        return ai_service.scoring_model_rejection(1, "anthropic", "claude-fable-5", db=None)

    @pytest.mark.asyncio
    async def test_a_refusing_model_is_named_and_turned_down(self, monkeypatch):
        create = AsyncMock(side_effect=_ApiError(400, "thinking cannot be disabled"))
        rejection = await self._probe(monkeypatch, create)
        assert rejection is not None
        assert "claude-fable-5" in rejection

    @pytest.mark.asyncio
    async def test_an_empty_answer_counts_too(self, monkeypatch):
        """The other shape of the same fault: the parameter went through and the
        model still wrote nothing at a scoring-sized budget."""
        resp = SimpleNamespace(
            stop_reason="max_tokens",
            content=[SimpleNamespace(type="thinking", thinking="")],
        )
        rejection = await self._probe(monkeypatch, AsyncMock(return_value=resp))
        assert rejection is not None
        assert "cannot score" in rejection

    @pytest.mark.asyncio
    async def test_a_working_model_passes(self, monkeypatch):
        resp = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="1")],
            usage=SimpleNamespace(input_tokens=12, output_tokens=1),
            stop_reason="end_turn",
        )
        assert await self._probe(monkeypatch, AsyncMock(return_value=resp)) is None

    @pytest.mark.asyncio
    async def test_a_rate_limit_does_not_block_the_save(self, monkeypatch):
        create = AsyncMock(side_effect=_ApiError(429, "rate limit"))
        assert await self._probe(monkeypatch, create) is None

    @pytest.mark.asyncio
    async def test_a_wrong_key_does_not_block_the_save(self, monkeypatch):
        """It says nothing about the model, and the key may be the next thing fixed."""
        create = AsyncMock(side_effect=_ApiError(401, "invalid x-api-key"))
        assert await self._probe(monkeypatch, create) is None

    @pytest.mark.asyncio
    async def test_a_hanging_provider_does_not_block_the_save(self, monkeypatch):
        async def never_answers(**kwargs):
            await asyncio.sleep(60)

        monkeypatch.setattr(ai_service, "_PROBE_TIMEOUT_SECONDS", 0.01)
        assert await self._probe(monkeypatch, never_answers) is None

    @pytest.mark.asyncio
    async def test_no_key_saved_yet_cannot_tell(self, monkeypatch):
        create = AsyncMock(side_effect=AssertionError("must not be called"))
        assert await self._probe(monkeypatch, create, key=None) is None

    @pytest.mark.asyncio
    async def test_an_empty_slot_asks_nothing(self, monkeypatch):
        async def fake_get_api_key(user_id, provider, db):
            raise AssertionError("must not be called")

        monkeypatch.setattr(ai_service, "get_api_key", fake_get_api_key)
        assert await ai_service.scoring_model_rejection(1, None, None, db=None) is None
        assert await ai_service.scoring_model_rejection(1, "anthropic", None, db=None) is None


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

    def test_an_ordinary_news_article_gets_the_floor(self):
        """The case this floor was raised for: a ~5k-character story, which is what
        most feeds carry. The ratio does not overtake the floor until well past it,
        so the floor — not the ratio — is what a typical summary is written into."""
        assert _summary_token_budget("x" * 5_000) == _SUMMARY_MIN_TOKENS

    def test_the_floor_fits_a_sectioned_summary(self):
        """The prompt allows a few labelled sections, which cost more than the same
        content as one paragraph. A floor sized for prose alone cut them off."""
        assert _SUMMARY_MIN_TOKENS >= 700


class TestDefaultSummaryPrompt:
    """The prompt is the only thing holding summary length down: the models expand to
    fill whatever cap they are given, so raising the cap alone just moves where the
    summary gets cut off. A qualitative bound was not enough either — the same prompt
    that Sonnet 4.6 read as ~126 words, Opus 5 read as ~200 — hence a word count."""

    def test_it_names_a_number(self):
        assert "150 words" in ai_service._DEFAULT_SUMMARY_PROMPT

    def test_the_numbers_are_ceilings_not_targets(self):
        """Otherwise every summary grows to meet them, short pieces included."""
        prompt = ai_service._DEFAULT_SUMMARY_PROMPT.lower()
        assert "ceilings rather than targets" in prompt

    def test_the_length_still_scales_up_for_a_long_article(self):
        """A feature cannot be summarized in the same breath as a news brief, and the
        token budget goes on scaling past the floor for exactly that case. A single
        flat cap would have made that scaling pointless."""
        prompt = ai_service._DEFAULT_SUMMARY_PROMPT.lower()
        assert "a sentence or two for a brief item" in prompt
        assert "up to 300 for a long feature" in prompt

    def test_it_bounds_the_length_against_the_article_too(self):
        """The word count alone says nothing about a very short article."""
        assert "small fraction of the original" in ai_service._DEFAULT_SUMMARY_PROMPT

    def test_it_asks_for_one_list_rather_than_several_sections(self):
        """The shape difference between the two models, not just the word count:
        Opus 5 split the same summary across two labelled sections where Sonnet used
        one list."""
        prompt = ai_service._DEFAULT_SUMMARY_PROMPT.lower()
        assert "one short list" in prompt
        assert "rather than splitting the summary across several labelled sections" in prompt

    def test_markdown_stays_allowed(self):
        """Length is bounded, formatting is not — the list is wanted, not tolerated."""
        assert "markdown" in ai_service._DEFAULT_SUMMARY_PROMPT.lower()

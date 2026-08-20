"""Unit tests for AI cost estimation (_calc_cost), incl. the provider fallback
used when a configured model isn't in the price catalog (free-text model field)."""
from __future__ import annotations

from app.services.ai_service import _PROVIDER_FALLBACK_MODEL
from app.services.stats_service import _calc_cost


def test_known_model_exact_cost_not_estimated():
    # Opus 4.8: $5/M in, 5x multiplier -> $25/M out. 1M in + 1M out = $30.
    cost, estimated = _calc_cost("claude-opus-4-8", "anthropic", 1_000_000, 1_000_000)
    assert cost == 30.0
    assert estimated is False


def test_versioned_alias_is_priced_not_estimated():
    # Dated snapshot maps to its alias via _MODEL_ALIAS_MAP.
    cost, estimated = _calc_cost("gpt-4o-mini-2024-07-18", "openai", 1_000_000, 0)
    assert cost == 0.15
    assert estimated is False


def test_unpriced_model_known_provider_falls_back_and_flags():
    # gpt-9-turbo isn't in the catalog; openai fallback is gpt-5.4 ($2.50/M in,
    # 6x -> $15/M out). 1M in + 1M out = $17.50, flagged estimated.
    cost, estimated = _calc_cost("gpt-9-turbo", "openai", 1_000_000, 1_000_000)
    assert cost == 17.5
    assert estimated is True


def test_fallback_matches_provider_representative_model():
    for provider, rep_model in _PROVIDER_FALLBACK_MODEL.items():
        unpriced = _calc_cost("made-up-model-xyz", provider, 1_000_000, 500_000)
        reference = _calc_cost(rep_model, provider, 1_000_000, 500_000)
        assert unpriced[0] == reference[0]
        assert unpriced[0] is not None
        assert unpriced[1] is True   # flagged estimated
        assert reference[1] is False  # the real model is priced directly


def test_unpriced_model_unknown_provider_returns_none():
    assert _calc_cost("some-local-llama", "ollama", 1_000_000, 1_000_000) == (None, False)


def test_unpriced_model_no_provider_returns_none():
    assert _calc_cost("weird-model", None, 1_000_000, 1_000_000) == (None, False)


def test_missing_model_returns_none():
    assert _calc_cost(None, "openai", 100, 100) == (None, False)
    assert _calc_cost("", "openai", 100, 100) == (None, False)


def test_custom_provider_never_priced_even_under_a_known_model_name():
    # The regression this guards: proxies (LiteLLM, vLLM) serve local models under
    # borrowed names, and the catalog is consulted by model name before any
    # provider fallback. Left to itself, a run on someone's own GPU would be
    # billed at OpenAI's rate and shown as an exact figure, not an estimate.
    assert _calc_cost("gpt-4o", "custom", 1_000_000, 1_000_000) == (None, False)
    assert _calc_cost("claude-opus-5", "custom", 1_000_000, 1_000_000) == (None, False)
    assert _calc_cost("qwen3:1.7b", "custom", 1_000_000, 1_000_000) == (None, False)


def test_same_model_is_priced_on_a_real_provider():
    # The guard is about the provider, not about the model name being unusable.
    cost, estimated = _calc_cost("gpt-4o", "openai", 1_000_000, 1_000_000)
    assert cost is not None
    assert estimated is False


def test_mixed_setup_totals_only_the_paid_slot():
    # Scoring local, main on Anthropic: the total is the paid work alone rather
    # than None (nothing priced) or a number pretending the local run cost money.
    from app.services.stats_service import _sum_costs

    scoring = _calc_cost("qwen3:1.7b", "custom", 500_000, 10_000)[0]
    main = _calc_cost("claude-sonnet-5", "anthropic", 1_000_000, 100_000)[0]
    assert scoring is None
    assert main is not None
    assert _sum_costs([scoring, main]) == main

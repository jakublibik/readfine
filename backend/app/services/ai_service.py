"""AI provider abstraction: client factory, verification, and core AI calls."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import UserAiKey
from app.models.user import UserSettings
from app.utils.crypto import decrypt, encrypt
from app.utils.text import strip_html
from app.utils.url_validator import async_validate_ai_endpoint_url

logger = logging.getLogger(__name__)


class Completion(NamedTuple):
    """One answer from a provider, with what it cost and whether it finished.

    ``truncated`` is True when the model stopped on the token cap rather than
    because it was done, so a caller storing the text can mark it as cut off
    instead of passing it off as complete. Most callers have nothing to do with
    it, which is the reason for the named field: ``result.text`` reads the same
    everywhere, while a bare tuple made every one of them spell out a throwaway
    for a flag they never look at.
    """
    text: str
    input_tokens: int
    output_tokens: int
    truncated: bool = False


class ProviderEmptyResponse(Exception):
    """Raised when an AI provider returns no usable text (blocked/empty/filtered),
    so callers handle it as a controlled error instead of crashing on .strip()."""


class ModelCannotSkipThinking(Exception):
    """Raised for a model that reasons whether or not it is asked to, on the one
    call that cannot afford it: scoring answers in ten tokens, which such a model
    spends thinking before it writes anything.

    ``status_code`` is the provider's own: the refusal arrives as a 400, and
    carrying it here means the job retry policy reads this as the permanent
    client error it is (see ``ai_jobs.apply_job_failure``) instead of trying the
    same impossible request twice more.
    """

    status_code = 400

    def __init__(self, model: str):
        self.model = model
        super().__init__(
            f"{model} always reasons before it answers and cannot be told to skip it, "
            f"so nothing is left of the ten tokens a score gets. Choose another model "
            f"for the scoring slot."
        )


def _extract_text(provider: str, resp) -> str:
    """Safely pull the text out of a provider response, raising
    ProviderEmptyResponse when content is missing/empty."""
    text: str | None = None
    if provider == "anthropic":
        # First text block, not blocks[0]: on models where thinking runs without
        # being asked for (Opus 5, Sonnet 5, Fable 5) the response opens with a
        # thinking block, which carries no .text. Requests ask for thinking to be
        # off (_anthropic_create), but Fable 5 refuses to turn it off at all, so a
        # leading thinking block still turns up and the answer sits behind it.
        text = next(
            (getattr(b, "text", None)
             for b in (getattr(resp, "content", None) or [])
             if getattr(b, "type", None) == "text"),
            None,
        )
    elif provider in _OPENAI_WIRE:
        choices = getattr(resp, "choices", None) or []
        if choices:
            text = getattr(choices[0].message, "content", None)
    elif provider == "gemini":
        text = getattr(resp, "text", None)
    else:
        raise ValueError(f"Unknown provider: {provider}")
    if not text or not text.strip():
        detail = _empty_response_detail(provider, resp)
        raise ProviderEmptyResponse(
            f"{provider} returned no usable content" + (f" ({detail})" if detail else "")
        )
    return text.strip()


def _empty_response_detail(provider: str, resp) -> str:
    """Why the response carried no text, for the error message and the banner.

    "no usable content" alone cannot tell a refusal from a hit token cap from a
    genuinely empty reply, which are three different things to do something about.
    Best-effort like _extract_truncated: a provider changing the shape of a field
    must not replace the real error with an AttributeError from the diagnostics.
    """
    try:
        if provider == "anthropic":
            blocks = getattr(resp, "content", None) or []
            block_types = [str(getattr(b, "type", "?")) for b in blocks]
            stop_reason = getattr(resp, "stop_reason", None)
            detail = f"stop_reason={stop_reason}, blocks=[{','.join(block_types)}]"
            # Reasoning we could not switch off ate the whole budget before the
            # answer began. Worth saying in words: this signature means the model
            # is wrong for the job rather than the key or the prompt being broken,
            # and it is what an always-thinking model does on the tightest budget
            # (scoring asks for a single decimal in 10 tokens). Keyed on the
            # response, not on a model name, so a future model needs no list entry.
            if stop_reason == "max_tokens" and "thinking" in block_types:
                detail += (
                    "; the model spent the whole token budget reasoning before "
                    "answering and cannot be told to skip it, so it is not suited "
                    "to this slot"
                )
            return detail
        if provider in _OPENAI_WIRE:
            choices = getattr(resp, "choices", None) or []
            reason = getattr(choices[0], "finish_reason", None) if choices else None
            return f"finish_reason={reason}"
        if provider == "gemini":
            candidates = getattr(resp, "candidates", None) or []
            reason = getattr(candidates[0], "finish_reason", None) if candidates else None
            # A prompt refused up front has no candidate at all; the reason for
            # that lives on prompt_feedback instead.
            blocked = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
            detail = f"finish_reason={getattr(reason, 'name', reason)}"
            return f"{detail}, block_reason={getattr(blocked, 'name', blocked)}" if blocked else detail
    except Exception:  # noqa: BLE001 — diagnostics must never mask the real failure
        return ""
    return ""


def _extract_truncated(provider: str, resp) -> bool:
    """True when the provider stopped generating because it hit the token cap.

    Best-effort by design: a missing or unrecognised stop reason reads as "not
    truncated", so a provider changing the shape of this field can never turn an
    otherwise good completion into a failure.
    """
    try:
        if provider == "anthropic":
            return getattr(resp, "stop_reason", None) == "max_tokens"
        if provider in _OPENAI_WIRE:
            choices = getattr(resp, "choices", None) or []
            return bool(choices) and getattr(choices[0], "finish_reason", None) == "length"
        if provider == "gemini":
            candidates = getattr(resp, "candidates", None) or []
            if not candidates:
                return False
            reason = getattr(candidates[0], "finish_reason", None)
            # google-genai returns an enum; its .name is stable across the enum
            # and plain-string representations the SDK has used over versions.
            return getattr(reason, "name", None) == "MAX_TOKENS"
    except Exception:  # noqa: BLE001 — never let a stop-reason quirk fail a completion
        return False
    return False


# Docs URLs shown next to the model input field. "custom" points at our own help
# page instead: which models exist there is a question about the user's own
# server, so the link that is worth offering is the setup guide.
PROVIDER_DOCS_URLS: dict[str, str] = {
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/models",
    "openai": "https://platform.openai.com/docs/models",
    "gemini": "https://ai.google.dev/gemini-api/docs/models/gemini",
    "custom": "/help#custom-endpoint",
}

SUPPORTED_PROVIDERS = list(PROVIDER_DOCS_URLS.keys())

# How each provider writes its own name. The stored identifier is lowercase and
# cannot be capitalised into these ("Openai"), so the UI reads them from here.
PROVIDER_LABELS: dict[str, str] = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "custom": "Custom (OpenAI-compatible)",
}

# Providers that speak the OpenAI protocol on the wire. "custom" is any
# OpenAI-compatible server (Ollama, llama.cpp, vLLM, LiteLLM, OpenRouter), so
# every branch that shapes a request or reads a response treats it like OpenAI.
# It is deliberately not the same thing as *being* OpenAI: the key, the price
# list and the docs link all belong to the provider identity, not to the wire
# format, which is why this is a separate tuple rather than a wider equality.
_OPENAI_WIRE = ("openai", "custom")


def provider_requires_key(provider: str | None) -> bool:
    """Whether a missing API key means this provider is unusable.

    A local model has no key to give, and treating "no key" as "not configured"
    would make Ollama look broken everywhere at once: no client, Verify refusing
    before it tries, and the interest profile reporting a missing key forever.
    One function so the next place that asks this question gets the same answer.
    """
    return provider != "custom"

# Input token cost in USD per 1M tokens.
# !! Update manually when providers change pricing !!
# Last updated: 2026-08-12
# Anthropic: https://www.anthropic.com/pricing
# OpenAI:    https://openai.com/api/pricing
# Gemini:    https://ai.google.dev/gemini-api/docs/pricing
_MODEL_INPUT_COST_PER_M: dict[str, float] = {
    # Anthropic
    "claude-haiku-4-5": 1.00,
    "claude-haiku-3-5": 0.80,
    # The $2 launch price was announced as introductory through 2026-08-31; the
    # rise to $3 was called off and $2 is now the standard price.
    "claude-sonnet-5": 2.00,
    "claude-sonnet-4-6": 3.00,
    "claude-sonnet-3-5": 3.00,
    "claude-opus-5": 5.00,
    "claude-opus-4-8": 5.00,
    "claude-opus-4-7": 5.00,
    "claude-opus-4-6": 5.00,
    "claude-opus-4-5": 5.00,
    "claude-fable-5": 10.00,
    # OpenAI
    "gpt-4o-mini": 0.15,
    "gpt-4o": 2.50,
    "gpt-5.5": 5.00,
    "gpt-5.4": 2.50,
    "gpt-5.4-mini": 0.75,
    "gpt-5.4-nano": 0.20,
    # Gemini
    "gemini-2.0-flash": 0.10,
    "gemini-2.0-flash-lite": 0.075,
    "gemini-1.5-flash": 0.075,
    "gemini-1.5-pro": 1.25,
    "gemini-2.5-pro": 1.25,
    "gemini-2.5-flash": 0.30,
    "gemini-2.5-flash-lite": 0.10,
    "gemini-3.6-flash": 1.50,
    "gemini-3.5-flash": 1.50,
    "gemini-3.5-flash-lite": 0.30,
    "gemini-3.1-flash-lite": 0.25,
    "gemini-3.1-pro-preview": 2.00,
}

# Map versioned IDs → alias so cost lookup works for both input formats
_MODEL_ALIAS_MAP: dict[str, str] = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-haiku-3-5-20241022": "claude-haiku-3-5",
    "claude-sonnet-3-5-20241022": "claude-sonnet-3-5",
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    "gpt-4o-2024-11-20": "gpt-4o",
}

# Output token cost = input cost × multiplier (output is more expensive than input)
_OUTPUT_COST_MULTIPLIER: dict[str, float] = {
    "claude-haiku-4-5": 5.00,
    "claude-haiku-3-5": 5.00,
    "claude-sonnet-5": 5.00,
    "claude-sonnet-4-6": 5.00,
    "claude-sonnet-3-5": 5.00,
    "claude-opus-5": 5.00,
    "claude-opus-4-8": 5.00,
    "claude-opus-4-7": 5.00,
    "claude-opus-4-6": 5.00,
    "claude-opus-4-5": 5.00,
    "claude-fable-5": 5.00,
    "gpt-4o-mini": 4.00,
    "gpt-4o": 4.00,
    "gpt-5.5": 6.00,  # $5.00 in / $30.00 out
    "gpt-5.4": 6.00,  # $2.50 in / $15.00 out
    "gpt-5.4-mini": 6.00,  # $0.75 in / $4.50 out
    "gpt-5.4-nano": 6.25,  # $0.20 in / $1.25 out
    "gemini-2.0-flash": 4.00,
    "gemini-2.0-flash-lite": 4.00,
    "gemini-1.5-flash": 4.00,
    "gemini-1.5-pro": 4.00,
    "gemini-2.5-pro": 8.00,
    "gemini-2.5-flash": 2.50 / 0.30,  # $0.30 in / $2.50 out
    "gemini-2.5-flash-lite": 4.00,  # $0.10 in / $0.40 out
    "gemini-3.6-flash": 5.00,  # $1.50 in / $7.50 out
    "gemini-3.5-flash": 6.00,  # $1.50 in / $9.00 out
    "gemini-3.5-flash-lite": 2.50 / 0.30,  # $0.30 in / $2.50 out
    "gemini-3.1-flash-lite": 6.00,  # $0.25 in / $1.50 out
    "gemini-3.1-pro-preview": 6.00,  # $2.00 in / $12.00 out
}

# When the configured model isn't in the catalog above (the model field is free
# text), estimate its cost using a representative mid-tier model for the provider.
# Cost rows priced this way are flagged is_estimated → rendered with a "~" prefix
# and a table note.
_PROVIDER_FALLBACK_MODEL: dict[str, str] = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-5.4",
    "gemini": "gemini-2.5-flash",
}

# ── key management ────────────────────────────────────────────────────────────

async def get_api_key(user_id: int, provider: str, db: AsyncSession) -> str | None:
    row = await db.scalar(
        select(UserAiKey).where(UserAiKey.user_id == user_id, UserAiKey.provider == provider)
    )
    if row is None:
        return None
    try:
        return decrypt(row.api_key_encrypted)
    except ValueError:
        logger.error("Failed to decrypt AI key for user=%s provider=%s", user_id, provider)
        return None


async def save_api_key(user_id: int, provider: str, api_key: str, db: AsyncSession) -> None:
    encrypted = encrypt(api_key)
    prefix = api_key[:8]
    row = await db.scalar(
        select(UserAiKey).where(UserAiKey.user_id == user_id, UserAiKey.provider == provider)
    )
    if row:
        row.api_key_encrypted = encrypted
        row.key_prefix = prefix
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(UserAiKey(user_id=user_id, provider=provider, api_key_encrypted=encrypted, key_prefix=prefix))
    await db.commit()


async def delete_api_key(user_id: int, provider: str, db: AsyncSession) -> None:
    row = await db.scalar(
        select(UserAiKey).where(UserAiKey.user_id == user_id, UserAiKey.provider == provider)
    )
    if row:
        await db.delete(row)
        await db.commit()


async def list_api_keys(user_id: int, db: AsyncSession) -> dict[str, str | None]:
    """Return {provider: key_prefix_or_None} for all supported providers."""
    rows = await db.scalars(select(UserAiKey).where(UserAiKey.user_id == user_id))
    saved = {r.provider: r.key_prefix for r in rows}
    return {p: saved.get(p) for p in SUPPORTED_PROVIDERS}


# ── client factory ────────────────────────────────────────────────────────────

def _make_anthropic_client(api_key: str):
    from anthropic import AsyncAnthropic
    return AsyncAnthropic(api_key=api_key)


def _make_openai_client(api_key: str):
    from openai import AsyncOpenAI
    return AsyncOpenAI(api_key=api_key)


# Sent as the key when the endpoint does not want one. The OpenAI SDK refuses to
# be constructed without a key at all, and a local server ignores whatever
# arrives in the header, so the request needs *something* that is obviously not a
# credential rather than an empty string that reads like a bug.
_NO_KEY_PLACEHOLDER = "not-needed"


# Spelled out because handing the SDK an http_client puts the timeout in our
# hands, and a generated answer is slow: a local model writing a summary on CPU
# runs for minutes, so the read budget has to be minutes too. The SDK's own
# default happens to be these numbers, and it applies them only while the client
# it is given still carries httpx's 5-second default — set that to anything else
# and the SDK steps aside, so a well-meant `timeout=30` here would cut every
# local summary short with nothing to point at why. Stating it leaves nothing to
# infer. Connect stays short: reaching the server is either immediate or wrong.
_CUSTOM_CLIENT_TIMEOUT = (5.0, 600.0)


def _make_custom_client(api_key: str | None, base_url: str):
    """Client for an OpenAI-compatible endpoint that is not OpenAI.

    The transport re-validates and pins every request (see
    :class:`PinnedAsyncTransport`). Validating the URL when it was saved cannot
    cover a request made later — DNS can be repointed at a private address in
    between — and the SDK opens its own connections, so the check has to live
    where those connections are made.
    """
    from openai import AsyncOpenAI
    import httpx

    from app.utils.url_validator import PinnedAsyncTransport

    connect, rest = _CUSTOM_CLIENT_TIMEOUT
    return AsyncOpenAI(
        api_key=api_key or _NO_KEY_PLACEHOLDER,
        base_url=base_url,
        http_client=httpx.AsyncClient(
            transport=PinnedAsyncTransport(),
            timeout=httpx.Timeout(connect=connect, read=rest, write=rest, pool=rest),
        ),
    )


def _make_gemini_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def _make_client(provider: str, api_key: str | None, base_url: str | None = None):
    """Build a provider client. *base_url* is required for (and only used by) custom."""
    if provider == "anthropic":
        return _make_anthropic_client(api_key)
    if provider == "openai":
        return _make_openai_client(api_key)
    if provider == "gemini":
        return _make_gemini_client(api_key)
    if provider == "custom":
        if not base_url:
            return None
        return _make_custom_client(api_key, base_url)
    return None


async def get_ai_client(user_id: int, slot: str, db: AsyncSession):
    """
    Return (client, provider, model) for the given slot ("fast" | "quality"),
    or (None, None, None) if not configured.
    """
    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None:
        return None, None, None

    if slot == "fast":
        provider, model = s.ai_fast_provider, s.ai_fast_model
    else:
        provider, model = s.ai_quality_provider, s.ai_quality_model

    if not provider or not model:
        return None, None, None

    api_key = await get_api_key(user_id, provider, db)
    if not api_key and provider_requires_key(provider):
        return None, None, None

    try:
        client = _make_client(provider, api_key, s.ai_custom_base_url)
        if client is None:
            # Either an unknown provider or custom without an endpoint to talk to.
            logger.warning("No AI client for provider=%s", provider)
            return None, None, None
    except Exception as exc:
        logger.error("Failed to create AI client provider=%s: %s", provider, exc)
        return None, None, None

    return client, provider, model


# ── verification ──────────────────────────────────────────────────────────────

# Room for a greeting, not for an essay. Deliberately more than a greeting needs:
# a model that cannot be told to skip reasoning (Fable 5) spends some of this
# before it writes anything, and failing the check there would report the whole
# slot as broken when only the tightest budget, scoring, actually is. Unused
# tokens are not billed, so the headroom is free.
_VERIFY_MAX_TOKENS = 200


def _friendly_ai_error(exc: Exception) -> str:
    raw = str(exc)
    low = raw.lower()
    if "not_found" in low or '"404"' in raw or " 404 " in raw:
        return "Model not found — check the model name."
    if "401" in raw or "authentication" in low or "invalid api key" in low or "unauthorized" in low:
        return "Invalid API key."
    if "429" in raw or "rate_limit" in low or "too many requests" in low:
        return "Rate limit reached — try again later."
    if "403" in raw or "forbidden" in low:
        return "Access denied — check your API key permissions."
    return raw.splitlines()[0][:150]


# How long the scoring probe below may hold up a settings save. The call itself is
# two tokens of prompt and ten of answer, so anything past this is the provider
# being slow rather than the model deliberating, and a slow provider is not a
# reason to refuse someone their settings.
_PROBE_TIMEOUT_SECONDS = 15


async def scoring_model_rejection(
    user_id: int,
    provider: str | None,
    model: str | None,
    db: AsyncSession,
    base_url: str | None = None,
) -> str | None:
    """Why *model* cannot be used for scoring, or None if it can (or we cannot tell).

    Scoring is the one call with no room for reasoning, so a model that insists on
    it produces an empty answer on every article it touches. That is worth catching
    when the model is chosen rather than one article at a time, which is what this
    is for: the settings form calls it before storing a new fast model.

    Deliberately one-sided. Only the model answering for itself counts against it —
    a refusal to skip thinking, or an empty reply at a scoring-sized budget. A
    missing key, a rate limit, a timeout or an unreachable provider says nothing
    about the model, so it returns None and the save goes through. Wrongly blocking
    someone's settings over a provider hiccup is a worse failure than letting a bad
    model through, which the job path still catches and reports.

    *base_url* is the custom endpoint to probe. It is passed in rather than read
    from the database because the settings form calls this *before* it stores
    anything: on the save that first sets up a custom endpoint the stored value
    is still empty, and probing that would fail every first attempt. The caller
    is responsible for having validated it (see ``validate_ai_endpoint_url``) —
    this sends a real request to it.
    """
    if not provider or not model:
        return None
    api_key = await get_api_key(user_id, provider, db)
    if not api_key and provider_requires_key(provider):
        return None
    try:
        client = _make_client(provider, api_key, base_url)
        if client is None:
            return None
        await asyncio.wait_for(
            _complete(
                "Reply with the digit 1 and nothing else.",
                client, provider, model,
                max_tokens=10, require_thinking_off=True,
            ),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except ModelCannotSkipThinking as exc:
        return str(exc)
    except ProviderEmptyResponse:
        # The other shape of the same fault: the parameter was accepted (or never
        # refused) and the model still wrote nothing at a scoring-sized budget.
        return (
            f"{model} answered a scoring-sized request with nothing at all, so it "
            f"cannot score articles. Choose another model for the scoring slot."
        )
    except Exception as exc:
        logger.info("Scoring probe for %s/%s was inconclusive: %s", provider, model, exc)
    return None


async def verify_ai_slot(
    user_id: int, slot: str, db: AsyncSession,
    provider_override: str | None = None,
    model_override: str | None = None,
    base_url_override: str | None = None,
) -> dict:
    """
    Send a minimal test call to verify the key and model are valid.
    Returns {"ok": bool, "model": str | None, "error": str | None}.
    provider/model/base_url overrides allow verifying unsaved form values.
    """
    client, provider, model = await get_ai_client(user_id, slot, db)
    if provider_override:
        provider = provider_override
    if model_override:
        model = model_override
    if provider_override or model_override or base_url_override:
        api_key = await get_api_key(user_id, provider, db)
        if not api_key and provider_requires_key(provider):
            return {"ok": False, "model": None, "error": "No API key saved for this provider."}
        base_url = base_url_override
        if provider == "custom" and not base_url:
            s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
            base_url = s.ai_custom_base_url if s else None
        if provider == "custom" and not base_url:
            return {"ok": False, "model": None, "error": "Enter the endpoint URL first."}
        if provider == "custom":
            try:
                await async_validate_ai_endpoint_url(base_url)
            except ValueError as exc:
                return {"ok": False, "model": None, "error": str(exc)}
        client = _make_client(provider, api_key, base_url)
    if client is None:
        return {"ok": False, "model": None, "error": "No provider/model/key configured for this slot."}

    try:
        if provider == "anthropic":
            resp = await _anthropic_create(
                client,
                model=model,
                max_tokens=_VERIFY_MAX_TOKENS,
                messages=[{"role": "user", "content": "Hi"}],
            )
        elif provider in _OPENAI_WIRE:
            # Thinking off for the check itself: it asks for a greeting, and a
            # local model that reasons first would spend the 200 tokens on that
            # and report the whole slot as broken when nothing is wrong with it.
            resp = await _openai_wire_create(
                client, provider, True,
                model=model,
                messages=[{"role": "user", "content": "Hi"}],
                **_openai_token_kwargs(provider, model, _VERIFY_MAX_TOKENS),
            )
        elif provider == "gemini":
            resp = await client.aio.models.generate_content(
                model=model,
                contents="Hi",
            )
        # Read the answer, don't just touch the envelope: a model that accepts the
        # request and then writes nothing (all of its budget spent reasoning) used
        # to pass this check, so the slot reported OK while every real call failed.
        _extract_text(provider, resp)
        return {"ok": True, "model": model, "error": None}
    except Exception as exc:
        return {"ok": False, "model": model, "error": _friendly_ai_error(exc)}


# ── AI calls ──────────────────────────────────────────────────────────────────

async def score_article(
    content: str, preference_text: str, client, provider: str, model: str
) -> tuple[float, int, int]:
    """Score article relevance 0.0–1.0. Returns (score, input_tokens, output_tokens)."""
    prompt = (
        f"Rate how relevant this article is to the reader based on their interest profile.\n"
        f"Score from 0.0 (no interest / actively avoid) to 1.0 (exactly what they want to read).\n\n"
        f"Reader profile:\n{preference_text}\n\n"
        f"---\n"
        f"Article:\n{content}\n\n"
        f"Reply with only a decimal number between 0.0 and 1.0."
    )
    answer = await _complete(
        prompt, client, provider, model, max_tokens=10, require_thinking_off=True
    )
    raw = answer.text
    # Extract the first decimal number — tolerates models that wrap the score in
    # prose ("0.8 - relevant", "Score: 0.7"). A truly unparseable response raises,
    # so the caller's retry/failure path handles it instead of silently scoring 0.5.
    match = re.search(r"\d*\.?\d+", raw or "")
    if match is None:
        raise ValueError(f"score_article: no number in AI response {raw!r}")
    score = float(match.group())
    return max(0.0, min(1.0, score)), answer.input_tokens, answer.output_tokens


_DEFAULT_SUMMARY_PROMPT = "Summarize the article, scaling the length with it: a sentence or two for a brief item, about 150 words for an ordinary article, and up to 300 for a long feature. Treat those as ceilings rather than targets, and stay well under them when the article is thin. Lead with prose covering the main point, and where the article is complex follow it with one short list of the key facts rather than splitting the summary across several labelled sections. A summary is always a small fraction of the original, so never let it approach the length of the article itself. Capture the conclusions and the context that changes how the article reads, and leave out detail that does not. Preserve meaningful nuance and uncertainty when relevant.\n\nAvoid filler, repetition, marketing language, and openings like \"This article explains…\". Focus on what matters most. Do not invent information. Respond in the same language as the article. You may use markdown (bold, a short list) where it genuinely aids clarity."
_DEFAULT_CONTEXT_PROMPT = "Explain the broader context and significance of this article. Adjust the length to what is genuinely needed — a sentence or two for straightforward topics, a short paragraph for complex ones. Cover what the reader should know to understand why this matters: relevant background, ongoing developments, or wider implications.\n\nAvoid filler, repetition, and openings like \"This article is about…\". Stick to what is relevant and well-founded — do not speculate or present uncertain claims as facts. Respond in the same language as the article. You may use markdown (bold, lists) where it genuinely aids clarity."
# The summary prompt tells the model to scale length with the article, so the
# output cap scales with it too — a cap sized for a news brief cuts a long feature
# off mid-sentence. Roughly one output token per 16 input characters, bounded at
# both ends: the floor keeps short articles from getting a uselessly tight cap,
# the ceiling stops a custom prompt asking for an essay from running up the bill.
#
# The floor is what most news articles actually get: the ratio only overtakes it
# past ~11k characters, and a typical story is half that. Length is held down by
# the prompt's 150-word ceiling, roughly 200 tokens, so the floor is deliberately
# loose on top of it. That slack is the point: these models read a length rule
# generously, and one that overshoots should still land a whole summary rather
# than a truncated one. Unused tokens are not billed, so the slack itself is free.
_SUMMARY_MIN_TOKENS = 700
_SUMMARY_MAX_TOKENS = 1500
_SUMMARY_CHARS_PER_TOKEN = 16


def _summary_token_budget(content: str) -> int:
    """Output-token cap for summarizing *content*."""
    return max(
        _SUMMARY_MIN_TOKENS,
        min(_SUMMARY_MAX_TOKENS, len(content) // _SUMMARY_CHARS_PER_TOKEN),
    )


async def summarize_article(
    content: str,
    client,
    provider: str,
    model: str,
    custom_prompt: str | None = None,
) -> Completion:
    """Generate a concise article summary.

    The only caller that acts on ``truncated``: a summary cut off by the token cap
    is still stored, but labelled as such rather than passed off as the model's own
    choice of ending.
    """
    instruction = custom_prompt or _DEFAULT_SUMMARY_PROMPT
    prompt = f"{instruction}\n\nArticle:\n{content}"
    return await _complete(
        prompt, client, provider, model,
        max_tokens=_summary_token_budget(content),
        reasoning_headroom=_ANTHROPIC_REASONING_BUDGET,
    )


async def get_article_context(
    content: str,
    client,
    provider: str,
    model: str,
    base_prompt: str | None = None,
    focus: str | None = None,
) -> tuple[str, int, int]:
    """Generate background context and significance. Returns (text, input_tokens, output_tokens)."""
    instruction = base_prompt or _DEFAULT_CONTEXT_PROMPT
    if focus:
        instruction += f"\n\nFocus on: {focus}"
    prompt = f"{instruction}\n\nArticle:\n{content}"
    answer = await _complete(prompt, client, provider, model, max_tokens=500)
    return answer.text, answer.input_tokens, answer.output_tokens


async def chat_with_article(
    messages: list[dict],
    article_content: str | None,
    client,
    provider: str,
    model: str,
) -> tuple[str, int, int]:
    """Multi-turn chat. Returns (text, input_tokens, output_tokens)."""
    if article_content:
        system_prompt = (
            "You are a helpful assistant discussing the following article. "
            "Answer questions based on the article content.\n\n"
            f"Article:\n{article_content}"
        )
    else:
        system_prompt = None

    if provider == "anthropic":
        kwargs: dict = dict(
            model=model,
            max_tokens=600,
            messages=[{"role": m["role"], "content": m["content"]} for m in messages],
        )
        if system_prompt:
            kwargs["system"] = system_prompt
        resp = await _anthropic_create(client, **kwargs)
        return (
            _extract_text("anthropic", resp),
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )

    elif provider in _OPENAI_WIRE:
        openai_msgs = []
        if system_prompt:
            openai_msgs.append({"role": "system", "content": system_prompt})
        openai_msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
        resp = await client.chat.completions.create(
            model=model, messages=openai_msgs,
            **_openai_token_kwargs(provider, model, 600))
        return (
            _extract_text(provider, resp),
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
        )

    elif provider == "gemini":
        from google.genai import types
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in messages
        ]
        cfg = types.GenerateContentConfig(
            max_output_tokens=600,
            system_instruction=system_prompt,
        )
        resp = await client.aio.models.generate_content(
            model=model, config=cfg, contents=contents)
        meta = resp.usage_metadata
        return (
            _extract_text("gemini", resp),
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
        )

    raise ValueError(f"Unknown provider: {provider}")


_DEFAULT_CATCHUP_PROMPT = (
    "Group the following articles by topic and write a concise digest. For each topic, "
    "use a short heading and write 2–4 sentences summarizing the key developments. Adjust the "
    "number of topics and sentences to what the content genuinely warrants.\n\n"
    "Avoid filler, repetition, and invented information. Do not speculate beyond what the "
    "articles suggest. Respond in the same language as the majority of the article titles. "
    "You may use markdown (bold, lists) where it genuinely aids clarity."
)


async def catch_me_up(
    articles_meta: list[dict],
    period: str,
    client,
    provider: str,
    model: str,
    custom_prompt: str | None = None,
) -> tuple[str, int, int]:
    """Generate a catch-up digest grouped by topic.

    Returns (text, input_tokens, output_tokens).
    articles_meta items: {"feed": str, "title": str, "date": str, "snippet": str (optional)}
    """
    system_prompt = custom_prompt or _DEFAULT_CATCHUP_PROMPT

    lines = []
    for a in articles_meta:
        line = f"- [{a['feed']}] {a['title']} ({a['date']})"
        snippet = a.get("snippet", "")
        if snippet:
            line += f" — {snippet}"
        lines.append(line)

    article_list = "\n".join(lines)
    user_prompt = f"Articles from the past {period}:\n\n{article_list}"

    full_prompt = f"{system_prompt}\n\n{user_prompt}"
    answer = await _complete(full_prompt, client, provider, model, max_tokens=8000)
    return answer.text, answer.input_tokens, answer.output_tokens


async def generate_css_selector(url: str, html: str, client, provider: str, model: str) -> str:
    """Generate a CSS selector for article links from a page."""
    from app.utils.scrape_ai import generate_selector_prompt
    prompt = generate_selector_prompt(url, html)
    answer = await _complete(prompt, client, provider, model, max_tokens=200)
    return answer.text.strip().strip('`"\'').split('\n')[0].strip()


async def generate_css_selector_from_sample(
    url: str,
    sample: str,
    history: list[dict],
    client,
    provider: str,
    model: str,
) -> tuple[str, int, int]:
    """Generate CSS selector from pre-extracted HTML sample with optional refinement history."""
    from app.utils.scrape_ai import build_selector_prompt
    prompt = build_selector_prompt(url, sample, history)
    answer = await _complete(prompt, client, provider, model, max_tokens=200)
    selector = answer.text.strip().strip('`"\'').split('\n')[0].strip()
    return selector, answer.input_tokens, answer.output_tokens


# Longest behavioural lookback window used by the interest profile (G1). The retention
# trim/delete (purge_service T2) keeps engaged article stubs at least this long so the
# profile still sees their signal. Keep > the admin retention horizon max (120).
PROFILE_MAX_WINDOW_DAYS = 180


async def get_preference_strong_count(user_id: int, db: AsyncSession) -> int:
    """Return count of strong reading signals (g1 + g2) used for preference generation."""
    from sqlalchemy import text
    now = datetime.now(timezone.utc)
    g1 = await db.execute(text("""
        SELECT COUNT(*) FROM user_article_states uas
        WHERE uas.user_id = :uid
          AND uas.user_starred = true
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
    """), {"uid": user_id, "cutoff": now - timedelta(days=PROFILE_MAX_WINDOW_DAYS)})
    g2 = await db.execute(text("""
        SELECT COUNT(*) FROM user_article_states uas
        WHERE uas.user_id = :uid
          AND uas.user_starred = false
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
    """), {"uid": user_id, "cutoff": now - timedelta(days=120)})
    return int(g1.scalar() or 0) + int(g2.scalar() or 0)


# ── interest profile generation ─────────────────────────────────────────────

def _pref_snippet(ai_summary: str | None, readable: str | None,
                  content: str | None, limit: int = 300) -> str:
    """Up to `limit` chars of normalized text: ai_summary → readable_content → content."""
    for src in (ai_summary, readable, content):
        if src:
            return strip_html(src)[:limit]
    return ""


# Section headers in prompt order; keys map to behaviour groups G1–G3, P1, N1.
_PREF_SECTIONS = {
    "g1": "Read and starred (strongest signal)",
    "g2": "Read thoroughly without starring",
    "g3": "Starred but barely read (weak signal)",
    "p1": "Engaged despite low predicted relevance (boost these)",
    "n1": "Predicted highly relevant but consistently skipped (narrow these, not avoid)",
}

_PREF_INSTRUCTION = (
    "Based on the reader's recent reading behaviour below, generate a concise "
    "interest profile for article relevance scoring.\n\n"
    "The data is grouped by signal. Use each group as follows:\n"
    "- \"Read and starred\" / \"Read thoroughly\": the core of the reader's interests.\n"
    "- \"Starred but barely read\": weaker signal — the title appealed; moderate at most.\n"
    "- \"Engaged despite low predicted relevance\": topics the reader clearly values "
    "even though they look niche — make sure these are represented.\n"
    "- \"Predicted highly relevant but consistently skipped\": the reader is pickier "
    "here than the topic alone suggests. Only titles are shown (the reader never "
    "opened these), so judge by topic, not specifics. NARROW the related "
    "high-relevance topics with qualifiers. Do NOT move them to Avoid.\n\n"
    "Rules:\n"
    "- High relevance is only for RECURRING themes seen across multiple articles. A "
    "topic from a single article belongs in Moderate, or is omitted. Never list a niche "
    "one-off as high relevance.\n"
    "- Avoid lists only broad subject areas the reader is clearly not oriented toward, "
    "inferred from the contrast with their demonstrated interests — general areas only, "
    "never specific people, organizations, or one-off events. May be empty.\n"
    "- Be specific where the data supports it, but prefer a slightly broader topic over "
    "an overfit one-off.\n\n"
    "Output exactly three lines, nothing else:\n"
    "High relevance: [recurring core topics, with qualifiers where data shows pickiness]\n"
    "Moderate relevance: [topics of occasional or one-off interest]\n"
    "Avoid: [broad subject areas the reader is clearly not oriented toward, inferred "
    "from the contrast with their interests; general areas only; may be empty]"
)


def _build_preference_prompt(groups: dict[str, list[tuple[str, str]]], feeds_str: str) -> str:
    """Assemble the preference-generation prompt from grouped (title, snippet) rows.

    Pure function (no DB/AI) so it is unit-testable. Empty groups are omitted.
    """
    sections: list[str] = []
    for key, header in _PREF_SECTIONS.items():
        rows = groups.get(key) or []
        if not rows:
            continue
        lines = "\n".join(
            f"- {title}" + (f" — {snippet}" if snippet else "")
            for title, snippet in rows
        )
        sections.append(f"{header}:\n{lines}")
    data = (feeds_str + "\n\n".join(sections)).strip() or "(no reading history yet)"
    return f"{_PREF_INSTRUCTION}\n\n---\n{data}"


async def generate_preference_text(user_id: int, db: AsyncSession, client, provider: str, model: str) -> str:
    """Generate preference text from user's reading behaviour signals."""
    from sqlalchemy import text
    now = datetime.now(timezone.utc)
    cutoff_180 = now - timedelta(days=PROFILE_MAX_WINDOW_DAYS)
    cutoff_120 = now - timedelta(days=120)
    cutoff_90 = now - timedelta(days=90)

    def _pairs(rows) -> list[tuple[str, str]]:
        return [(r[0], _pref_snippet(r[1], r[2], r[3])) for r in rows]

    # G1: starred + read thoroughly or opened link (strongest signal)
    g1 = await db.execute(text("""
        SELECT a.title, uas.ai_summary, a.readable_content, a.content FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.user_starred = true
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
        ORDER BY uas.created_at DESC LIMIT 50
    """), {"uid": user_id, "cutoff": cutoff_180})
    g1_rows = _pairs(g1)

    # G2: read thoroughly or opened link, not starred
    g2 = await db.execute(text("""
        SELECT a.title, uas.ai_summary, a.readable_content, a.content FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.user_starred = false
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
        ORDER BY uas.created_at DESC LIMIT 30
    """), {"uid": user_id, "cutoff": cutoff_120})
    g2_rows = _pairs(g2)

    # G3: starred only, barely read (impulsive, weaker signal)
    g3 = await db.execute(text("""
        SELECT a.title, uas.ai_summary, a.readable_content, a.content FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.user_starred = true AND uas.dwell_seconds < 60
          AND uas.created_at >= :cutoff
        ORDER BY uas.created_at DESC LIMIT 20
    """), {"uid": user_id, "cutoff": cutoff_90})
    g3_rows = _pairs(g3)

    # P1: scoring under-rated these — low score but reader engaged (boost). Most
    # under-scored first.
    p1 = await db.execute(text("""
        SELECT a.title, uas.ai_summary, a.readable_content, a.content FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.ai_score IS NOT NULL AND uas.ai_score <= 0.4
          AND (uas.dwell_seconds >= 60 OR uas.user_starred = true)
          AND uas.created_at >= :cutoff
        ORDER BY uas.ai_score ASC LIMIT 15
    """), {"uid": user_id, "cutoff": cutoff_90})
    p1_rows = _pairs(p1)

    # N1: scoring over-rated these — high score but consistently ignored (refine).
    # Most over-scored first. Titles only (no snippet): the reader never opened
    # these, so the content they didn't see must not seed overfit Avoid items.
    n1 = await db.execute(text("""
        SELECT a.title FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.ai_score IS NOT NULL AND uas.ai_score >= 0.85
          AND uas.dwell_seconds = 0 AND uas.link_opened = false
          AND uas.created_at >= :cutoff
        ORDER BY uas.ai_score DESC LIMIT 15
    """), {"uid": user_id, "cutoff": cutoff_90})
    n1_rows = [(r[0], "") for r in n1]

    strong_count = len(g1_rows) + len(g2_rows)

    # Cold start fallback: include feed titles when behavioural data is sparse
    feeds_str = ""
    if strong_count < 20:
        feeds = await db.execute(text("""
            SELECT f.title FROM feeds f
            JOIN user_feeds uf ON uf.feed_id = f.id
            WHERE uf.user_id = :uid LIMIT 25
        """), {"uid": user_id})
        feed_titles = [r[0] for r in feeds]
        if feed_titles:
            feeds_str = "Subscribed feeds (general context):\n" + "\n".join(f"- {t}" for t in feed_titles) + "\n\n"

    prompt = _build_preference_prompt(
        {"g1": g1_rows, "g2": g2_rows, "g3": g3_rows, "p1": p1_rows, "n1": n1_rows},
        feeds_str,
    )
    answer = await _complete(prompt, client, provider, model, max_tokens=500)
    return answer.text, answer.input_tokens, answer.output_tokens


# ── internal ──────────────────────────────────────────────────────────────────

# OpenAI's o-series and gpt-5 family are reasoning models: reasoning tokens
# count against max_completion_tokens and are spent before any visible output,
# so a tight cap (e.g. 10 for scoring) can yield an empty response. Give them
# extra headroom on top of the desired output length. max_completion_tokens is
# only a ceiling — unused tokens are not billed — so this is free for short
# outputs on non-reasoning models, which keep their original cap unchanged.
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_OPENAI_REASONING_BUDGET = 8000


def _openai_max_tokens(model: str, max_tokens: int) -> int:
    """Add reasoning headroom for OpenAI reasoning models; others unchanged."""
    if (model or "").lower().startswith(_OPENAI_REASONING_PREFIXES):
        return max_tokens + _OPENAI_REASONING_BUDGET
    return max_tokens


# What an OpenAI-compatible server understands as "do not reason on this one".
# The three that matter all take it on /v1/chat/completions: Ollama (where it is
# the only way — the native think:false is not read on that endpoint, and Ollama
# turns thinking on by itself for models that can), llama.cpp's server, and vLLM,
# which forwards it to the chat template as enable_thinking=false.
#
# Only sent where reasoning actually breaks the call. Scoring answers with one
# number in ten tokens, so a model that reasons first spends all ten on that and
# comes back empty; summaries and chat have room for it and are left alone.
_OPENAI_REASONING_OFF = "none"


# A rejected request parameter arrives as 400 from most servers, but the
# OpenAI-compatible ones built on FastAPI (vLLM, a LiteLLM proxy) answer an
# unrecognised field with 422 instead, which is FastAPI's own validation status.
# Accepting only 400 would mean the retry never fires exactly where it is needed.
_PARAM_REJECTED_STATUSES = (400, 422)


def _rejects_reasoning_effort(exc: Exception) -> bool:
    """True when the error is about reasoning_effort rather than the request itself.

    Narrow on purpose, like _rejects_thinking_param: a server old enough not to
    know the parameter should get one retry without it, while a bad key, an
    unknown model or a rate limit stays the error it is. The status alone is not
    enough for that — the message has to name the parameter.
    """
    if getattr(exc, "status_code", None) not in _PARAM_REJECTED_STATUSES:
        return False
    text = str(exc).lower()
    return "reasoning_effort" in text or "reasoning effort" in text


async def _openai_wire_create(client, provider: str, require_thinking_off: bool, **kwargs):
    """chat.completions.create, asking a custom endpoint to skip reasoning.

    Not sent to OpenAI itself: there reasoning is paid for in headroom instead
    (see _openai_max_tokens), and its reasoning models refuse "none" outright.

    A server that does not know the parameter is retried without it rather than
    turned down. Unlike Anthropic, where refusing to disable thinking identifies a
    model that always thinks, here the refusal usually means the *server* is older
    than the parameter and says nothing about the model — which may well answer in
    ten tokens quite happily. If it does not, the empty answer is caught where
    every other empty answer is, and the scoring probe reports it when the model
    is chosen.
    """
    if provider != "custom" or not require_thinking_off:
        return await client.chat.completions.create(**kwargs)
    try:
        return await client.chat.completions.create(
            reasoning_effort=_OPENAI_REASONING_OFF, **kwargs
        )
    except Exception as exc:
        if not _rejects_reasoning_effort(exc):
            raise
        logger.info(
            "Endpoint rejected reasoning_effort=none for %s; retrying without it",
            kwargs.get("model"),
        )
        return await client.chat.completions.create(**kwargs)


def _openai_token_kwargs(provider: str, model: str, max_tokens: int) -> dict[str, int]:
    """How to spell the output cap for an OpenAI-protocol request.

    ``max_completion_tokens`` is OpenAI's current name for it, and OpenAI itself
    rejects the old ``max_tokens``. Compatible servers went the other way: the
    llama.cpp server and older Ollama builds only know ``max_tokens``, so a
    custom endpoint gets that spelling and would otherwise fail on its very first
    request.

    The reasoning headroom is OpenAI's too, and it is keyed on model-name
    prefixes that say nothing about a model served from someone's own machine, so
    custom asks for the budget it was given.
    """
    if provider == "custom":
        return {"max_tokens": max_tokens}
    return {"max_completion_tokens": _openai_max_tokens(model, max_tokens)}


# Anthropic's newer models (Opus 5, Sonnet 5, Fable 5) think even when the request
# says nothing about thinking, and those tokens come out of the same max_tokens as
# the answer. Every budget here is sized for the answer alone — scoring asks for a
# single decimal in 10 tokens — so the model would spend the budget thinking and
# return a response with no text block at all. The same problem on OpenAI is solved
# with headroom above, because there is no way to turn reasoning off; Anthropic can
# be asked directly, which keeps the budgets meaning what they say.
_ANTHROPIC_THINKING_OFF = {"type": "disabled"}


def _rejects_thinking_param(exc: Exception) -> bool:
    """True when a 400 is about the thinking parameter rather than the request.

    Deliberately narrow: only a 400 that names the parameter counts, so a bad key,
    a wrong model name or a rate limit still surfaces as itself instead of being
    silently retried.
    """
    return getattr(exc, "status_code", None) == 400 and "thinking" in str(exc).lower()


# Room added to the retry below for the answer to survive alongside reasoning we
# could not turn off. Same trade as _OPENAI_REASONING_BUDGET: max_tokens is only a
# ceiling, so unused tokens cost nothing, and a model that ignored the retry (an
# older one that simply does not know the parameter) keeps behaving exactly as it
# did. Only callers expecting a long answer ask for it — see reasoning_headroom.
_ANTHROPIC_REASONING_BUDGET = 8000


async def _anthropic_create(
    client, reasoning_headroom: int = 0, require_thinking_off: bool = False, **kwargs
):
    """messages.create with thinking off, retried by models that refuse that.

    Fable 5 always thinks and answers an explicit "disabled" with a 400, and models
    older than the parameter reject it too. Those two look identical here and need
    opposite things: the old model does not think, so its budget was never at risk,
    while Fable 5 spends the answer's budget reasoning and returns a summary cut off
    mid-sentence. The retry therefore carries *reasoning_headroom* on top of
    max_tokens, which rescues the second without changing the first.

    *require_thinking_off* is for the one caller the retry cannot help: scoring asks
    for a single decimal in 10 tokens, and an always-thinking model cannot answer
    that at any ceiling worth paying for. Rather than send a request that is known
    to come back empty, it raises ModelCannotSkipThinking and says so in words. Note
    that this fires for an old model that merely does not know the parameter too:
    that model would answer fine, but it is not one anybody is scoring with, and
    guessing which of the two we are talking to would take the wasted call the flag
    exists to avoid.
    """
    try:
        return await client.messages.create(thinking=_ANTHROPIC_THINKING_OFF, **kwargs)
    except Exception as exc:
        if not _rejects_thinking_param(exc):
            raise
        if require_thinking_off:
            raise ModelCannotSkipThinking(kwargs.get("model") or "This model") from exc
        logger.info("Model %s rejected thinking=disabled; retrying without it",
                    kwargs.get("model"))
        if reasoning_headroom and kwargs.get("max_tokens"):
            kwargs = {**kwargs, "max_tokens": kwargs["max_tokens"] + reasoning_headroom}
        return await client.messages.create(**kwargs)


async def _complete(
    prompt: str, client, provider: str, model: str, max_tokens: int = 500,
    reasoning_headroom: int = 0, require_thinking_off: bool = False,
) -> Completion:
    """Send a prompt to whichever provider the slot uses and return its answer.

    reasoning_headroom is Anthropic-only. require_thinking_off means "this budget
    has no room to think in", and each provider honours it in the way it can:
    Anthropic is told to disable thinking and declares defeat when it cannot (see
    _anthropic_create), a custom endpoint is sent reasoning_effort=none (see
    _openai_wire_create), and OpenAI is given headroom instead, since its
    reasoning models have no off switch (see _openai_max_tokens).
    """
    if provider == "anthropic":
        resp = await _anthropic_create(
            client,
            reasoning_headroom=reasoning_headroom,
            require_thinking_off=require_thinking_off,
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return Completion(
            _extract_text("anthropic", resp),
            resp.usage.input_tokens,
            resp.usage.output_tokens,
            _extract_truncated("anthropic", resp),
        )
    elif provider in _OPENAI_WIRE:
        resp = await _openai_wire_create(
            client, provider, require_thinking_off,
            model=model,
            messages=[{"role": "user", "content": prompt}],
            **_openai_token_kwargs(provider, model, max_tokens),
        )
        return Completion(
            _extract_text(provider, resp),
            resp.usage.prompt_tokens,
            resp.usage.completion_tokens,
            _extract_truncated(provider, resp),
        )
    elif provider == "gemini":
        from google.genai import types
        resp = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=max_tokens),
        )
        meta = resp.usage_metadata
        return Completion(
            _extract_text("gemini", resp),
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
            _extract_truncated("gemini", resp),
        )
    raise ValueError(f"Unknown provider: {provider}")

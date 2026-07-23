"""AI provider abstraction: client factory, verification, and core AI calls."""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import UserAiKey
from app.models.user import UserSettings
from app.utils.crypto import decrypt, encrypt
from app.utils.text import strip_html

logger = logging.getLogger(__name__)


class ProviderEmptyResponse(Exception):
    """Raised when an AI provider returns no usable text (blocked/empty/filtered),
    so callers handle it as a controlled error instead of crashing on .strip()."""


def _extract_text(provider: str, resp) -> str:
    """Safely pull the text out of a provider response, raising
    ProviderEmptyResponse when content is missing/empty."""
    text: str | None = None
    if provider == "anthropic":
        blocks = getattr(resp, "content", None) or []
        if blocks:
            # Assumes the first block is the text block. Holds for plain
            # completions; would need to scan for the text block if an
            # extended-thinking model is ever used (block[0] = thinking).
            text = getattr(blocks[0], "text", None)
    elif provider == "openai":
        choices = getattr(resp, "choices", None) or []
        if choices:
            text = getattr(choices[0].message, "content", None)
    elif provider == "gemini":
        text = getattr(resp, "text", None)
    else:
        raise ValueError(f"Unknown provider: {provider}")
    if not text or not text.strip():
        raise ProviderEmptyResponse(f"{provider} returned no usable content")
    return text.strip()


# Docs URLs shown next to the model input field
PROVIDER_DOCS_URLS: dict[str, str] = {
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/models",
    "openai": "https://platform.openai.com/docs/models",
    "gemini": "https://ai.google.dev/gemini-api/docs/models/gemini",
}

SUPPORTED_PROVIDERS = list(PROVIDER_DOCS_URLS.keys())

# Input token cost in USD per 1M tokens.
# !! Update manually when providers change pricing !!
# Last updated: 2026-07-07
# Anthropic: https://www.anthropic.com/pricing
# OpenAI:    https://openai.com/api/pricing
# Gemini:    https://ai.google.dev/gemini-api/docs/pricing
_MODEL_INPUT_COST_PER_M: dict[str, float] = {
    # Anthropic
    "claude-haiku-4-5": 1.00,
    "claude-haiku-3-5": 0.80,
    "claude-sonnet-5": 3.00,  # standard; intro $2 in/$10 out through 2026-08-31
    "claude-sonnet-4-6": 3.00,
    "claude-sonnet-3-5": 3.00,
    "claude-opus-4-8": 5.00,
    "claude-opus-4-7": 5.00,
    "claude-opus-4-6": 5.00,
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
    "gemini-3.5-flash": 1.50,
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
    "claude-opus-4-8": 5.00,
    "claude-opus-4-7": 5.00,
    "claude-opus-4-6": 5.00,
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
    "gemini-3.5-flash": 6.00,  # $1.50 in / $9.00 out
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


def _make_gemini_client(api_key: str):
    from google import genai
    return genai.Client(api_key=api_key)


def _make_client(provider: str, api_key: str):
    if provider == "anthropic":
        return _make_anthropic_client(api_key)
    if provider == "openai":
        return _make_openai_client(api_key)
    if provider == "gemini":
        return _make_gemini_client(api_key)
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
    if not api_key:
        return None, None, None

    try:
        if provider == "anthropic":
            client = _make_anthropic_client(api_key)
        elif provider == "openai":
            client = _make_openai_client(api_key)
        elif provider == "gemini":
            client = _make_gemini_client(api_key)
        else:
            logger.warning("Unknown AI provider: %s", provider)
            return None, None, None
    except Exception as exc:
        logger.error("Failed to create AI client provider=%s: %s", provider, exc)
        return None, None, None

    return client, provider, model


# ── verification ──────────────────────────────────────────────────────────────

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


async def verify_ai_slot(
    user_id: int, slot: str, db: AsyncSession,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict:
    """
    Send a minimal test call to verify the key and model are valid.
    Returns {"ok": bool, "model": str | None, "error": str | None}.
    provider_override/model_override allow verifying unsaved form values.
    """
    client, provider, model = await get_ai_client(user_id, slot, db)
    if provider_override:
        provider = provider_override
    if model_override:
        model = model_override
    if provider_override or model_override:
        api_key = await get_api_key(user_id, provider, db)
        if not api_key:
            return {"ok": False, "model": None, "error": "No API key saved for this provider."}
        client = _make_client(provider, api_key)
    if client is None:
        return {"ok": False, "model": None, "error": "No provider/model/key configured for this slot."}

    try:
        if provider == "anthropic":
            resp = await client.messages.create(
                model=model,
                max_tokens=5,
                messages=[{"role": "user", "content": "Hi"}],
            )
            _ = resp.content
        elif provider == "openai":
            resp = await client.chat.completions.create(
                model=model,
                max_completion_tokens=_openai_max_tokens(model, 5),
                messages=[{"role": "user", "content": "Hi"}],
            )
            _ = resp.choices
        elif provider == "gemini":
            resp = await client.aio.models.generate_content(
                model=model,
                contents="Hi",
            )
            _ = resp.text
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
    raw, in_tok, out_tok = await _complete(prompt, client, provider, model, max_tokens=10)
    # Extract the first decimal number — tolerates models that wrap the score in
    # prose ("0.8 - relevant", "Score: 0.7"). A truly unparseable response raises,
    # so the caller's retry/failure path handles it instead of silently scoring 0.5.
    match = re.search(r"\d*\.?\d+", raw or "")
    if match is None:
        raise ValueError(f"score_article: no number in AI response {raw!r}")
    score = float(match.group())
    return max(0.0, min(1.0, score)), in_tok, out_tok


_DEFAULT_SUMMARY_PROMPT = "Summarize the article. Adjust the length naturally to the article's length and complexity — from one sentence for simple pieces to a short paragraph for complex ones. Capture the main point, key facts, conclusions, and important context or implications. Preserve meaningful nuance and uncertainty when relevant.\n\nAvoid filler, repetition, marketing language, and openings like \"This article explains…\". Focus on what matters most. Do not invent information. Respond in the same language as the article. You may use markdown (bold, lists) where it genuinely aids clarity."
_DEFAULT_CONTEXT_PROMPT = "Explain the broader context and significance of this article. Adjust the length to what is genuinely needed — a sentence or two for straightforward topics, a short paragraph for complex ones. Cover what the reader should know to understand why this matters: relevant background, ongoing developments, or wider implications.\n\nAvoid filler, repetition, and openings like \"This article is about…\". Stick to what is relevant and well-founded — do not speculate or present uncertain claims as facts. Respond in the same language as the article. You may use markdown (bold, lists) where it genuinely aids clarity."
async def summarize_article(
    content: str,
    client,
    provider: str,
    model: str,
    custom_prompt: str | None = None,
) -> tuple[str, int, int]:
    """Generate a concise article summary. Returns (text, input_tokens, output_tokens)."""
    instruction = custom_prompt or _DEFAULT_SUMMARY_PROMPT
    prompt = f"{instruction}\n\nArticle:\n{content}"
    return await _complete(prompt, client, provider, model, max_tokens=500)


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
    return await _complete(prompt, client, provider, model, max_tokens=500)


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
        resp = await client.messages.create(**kwargs)
        return (
            _extract_text("anthropic", resp),
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )

    elif provider == "openai":
        openai_msgs = []
        if system_prompt:
            openai_msgs.append({"role": "system", "content": system_prompt})
        openai_msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
        resp = await client.chat.completions.create(
            model=model, max_completion_tokens=_openai_max_tokens(model, 600),
            messages=openai_msgs)
        return (
            _extract_text("openai", resp),
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
    text, input_tokens, output_tokens = await _complete(
        full_prompt, client, provider, model, max_tokens=8000
    )
    return text, input_tokens, output_tokens


async def generate_css_selector(url: str, html: str, client, provider: str, model: str) -> str:
    """Generate a CSS selector for article links from a page."""
    from app.utils.scrape_ai import generate_selector_prompt
    prompt = generate_selector_prompt(url, html)
    text, _, _ = await _complete(prompt, client, provider, model, max_tokens=200)
    return text.strip().strip('`"\'').split('\n')[0].strip()


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
    text, in_tok, out_tok = await _complete(prompt, client, provider, model, max_tokens=200)
    selector = text.strip().strip('`"\'').split('\n')[0].strip()
    return selector, in_tok, out_tok


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
    result_text, input_tokens, output_tokens = await _complete(prompt, client, provider, model, max_tokens=500)
    return result_text, input_tokens, output_tokens


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


async def _complete(
    prompt: str, client, provider: str, model: str, max_tokens: int = 500
) -> tuple[str, int, int]:
    """Send a prompt and return (text, input_tokens, output_tokens)."""
    if provider == "anthropic":
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return _extract_text("anthropic", resp), resp.usage.input_tokens, resp.usage.output_tokens
    elif provider == "openai":
        resp = await client.chat.completions.create(
            model=model,
            max_completion_tokens=_openai_max_tokens(model, max_tokens),
            messages=[{"role": "user", "content": prompt}],
        )
        return _extract_text("openai", resp), resp.usage.prompt_tokens, resp.usage.completion_tokens
    elif provider == "gemini":
        from google.genai import types
        resp = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(max_output_tokens=max_tokens),
        )
        meta = resp.usage_metadata
        return (
            _extract_text("gemini", resp),
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
        )
    raise ValueError(f"Unknown provider: {provider}")

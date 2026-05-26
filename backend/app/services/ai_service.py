"""AI provider abstraction: client factory, verification, and core AI calls."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import UserAiKey
from app.models.user import UserSettings
from app.utils.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

# Docs URLs shown next to the model input field
PROVIDER_DOCS_URLS: dict[str, str] = {
    "anthropic": "https://docs.anthropic.com/en/docs/about-claude/models",
    "openai": "https://platform.openai.com/docs/models",
    "gemini": "https://ai.google.dev/gemini-api/docs/models/gemini",
}

SUPPORTED_PROVIDERS = list(PROVIDER_DOCS_URLS.keys())

# Input token cost in USD per 1M tokens (approximate, updated periodically)
# Keys are model aliases; versioned IDs are mapped to their alias below.
_MODEL_INPUT_COST_PER_M: dict[str, float] = {
    # Anthropic
    "claude-haiku-4-5": 0.80,
    "claude-haiku-3-5": 0.80,
    "claude-sonnet-4-6": 3.00,
    "claude-sonnet-3-5": 3.00,
    "claude-opus-4-7": 15.00,
    # OpenAI
    "gpt-4o-mini": 0.15,
    "gpt-4o": 2.50,
    # Gemini
    "gemini-2.0-flash": 0.10,
    "gemini-2.0-flash-lite": 0.075,
    "gemini-1.5-flash": 0.075,
    "gemini-1.5-pro": 1.25,
    "gemini-2.5-pro": 1.25,
}

# Map versioned IDs → alias so cost lookup works for both input formats
_MODEL_ALIAS_MAP: dict[str, str] = {
    "claude-haiku-4-5-20251001": "claude-haiku-4-5",
    "claude-haiku-3-5-20241022": "claude-haiku-3-5",
    "claude-sonnet-3-5-20241022": "claude-sonnet-3-5",
    "gpt-4o-mini-2024-07-18": "gpt-4o-mini",
    "gpt-4o-2024-11-20": "gpt-4o",
}

# Approximate tokens per article for cost estimation
_SCORING_TOKENS_PER_ARTICLE = 500
_SUMMARY_TOKENS_PER_ARTICLE = 1000


def estimate_cost_usd(model: str, tokens: int) -> float | None:
    """Return estimated USD cost for token count, or None if model is unknown."""
    key = _MODEL_ALIAS_MAP.get(model, model)
    cost_per_m = _MODEL_INPUT_COST_PER_M.get(key)
    if cost_per_m is None:
        return None
    return round(tokens * cost_per_m / 1_000_000, 4)


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
                max_tokens=5,
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
    try:
        score = float(raw.strip())
        return max(0.0, min(1.0, score)), in_tok, out_tok
    except ValueError:
        logger.warning("score_article: unexpected AI response %r, defaulting to 0.5", raw)
        return 0.5, in_tok, out_tok


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
            resp.content[0].text.strip(),
            resp.usage.input_tokens,
            resp.usage.output_tokens,
        )

    elif provider == "openai":
        openai_msgs = []
        if system_prompt:
            openai_msgs.append({"role": "system", "content": system_prompt})
        openai_msgs += [{"role": m["role"], "content": m["content"]} for m in messages]
        resp = await client.chat.completions.create(
            model=model, max_tokens=600, messages=openai_msgs)
        return (
            resp.choices[0].message.content.strip(),
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
        cfg = types.GenerateContentConfig(system_instruction=system_prompt) if system_prompt else None
        resp = await client.aio.models.generate_content(
            model=model, config=cfg, contents=contents)
        meta = resp.usage_metadata
        return (
            resp.text.strip(),
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
        )

    raise ValueError(f"Unknown provider: {provider}")


async def catch_me_up(articles_meta: list[dict], period: str, client, provider: str, model: str) -> str:
    """Generate a catch-up digest grouped by topic."""
    lines = [f"- [{a['feed']}] {a['title']} ({a['date']})" for a in articles_meta[:200]]
    article_list = "\n".join(lines)
    prompt = (
        f"Here are article headlines from the past {period}. "
        f"Group them by topic and write a brief digest (2–5 sentences per topic). "
        f"Focus on what's important.\n\n{article_list}"
    )
    text, _, _ = await _complete(prompt, client, provider, model, max_tokens=1000)
    return text


async def generate_css_selector(url: str, html: str, client, provider: str, model: str) -> str:
    """Generate a CSS selector for article links from a page."""
    from app.utils.scrape_ai import generate_selector_prompt
    prompt = generate_selector_prompt(url, html)
    text, _, _ = await _complete(prompt, client, provider, model, max_tokens=100)
    return text


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
    """), {"uid": user_id, "cutoff": now - timedelta(days=180)})
    g2 = await db.execute(text("""
        SELECT COUNT(*) FROM user_article_states uas
        WHERE uas.user_id = :uid
          AND uas.user_starred = false
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
    """), {"uid": user_id, "cutoff": now - timedelta(days=90)})
    return int(g1.scalar() or 0) + int(g2.scalar() or 0)


async def generate_preference_text(user_id: int, db: AsyncSession, client, provider: str, model: str) -> str:
    """Generate preference text from user's reading behaviour signals."""
    from sqlalchemy import text
    now = datetime.now(timezone.utc)
    cutoff_6m = now - timedelta(days=180)
    cutoff_3m = now - timedelta(days=90)
    cutoff_2m = now - timedelta(days=60)

    # Group 1: starred + read thoroughly or opened link (strongest signal)
    g1 = await db.execute(text("""
        SELECT a.title FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.user_starred = true
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
        ORDER BY uas.created_at DESC LIMIT 30
    """), {"uid": user_id, "cutoff": cutoff_6m})
    g1_titles = [r[0] for r in g1]

    # Group 2: read thoroughly or opened link, not starred
    g2 = await db.execute(text("""
        SELECT a.title FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.user_starred = false
          AND (uas.dwell_seconds >= 60 OR uas.link_opened = true)
          AND uas.created_at >= :cutoff
        ORDER BY uas.created_at DESC LIMIT 50
    """), {"uid": user_id, "cutoff": cutoff_3m})
    g2_titles = [r[0] for r in g2]

    # Group 3: starred only (impulsive, weaker signal)
    g3 = await db.execute(text("""
        SELECT a.title FROM articles a
        JOIN user_article_states uas ON uas.article_id = a.id
        WHERE uas.user_id = :uid
          AND uas.user_starred = true AND uas.dwell_seconds < 60
          AND uas.created_at >= :cutoff
        ORDER BY uas.created_at DESC LIMIT 20
    """), {"uid": user_id, "cutoff": cutoff_2m})
    g3_titles = [r[0] for r in g3]

    strong_count = len(g1_titles) + len(g2_titles)

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

    def _fmt(titles: list[str], label: str) -> str:
        if not titles:
            return ""
        lines = "\n".join(f"- {t}" for t in titles)
        return f"{label}:\n{lines}\n\n"

    data = (
        feeds_str
        + _fmt(g1_titles, "Articles starred and read thoroughly (strongest signal)")
        + _fmt(g2_titles, "Articles read thoroughly without starring")
        + _fmt(g3_titles, "Articles starred (title looked interesting, may not have been read fully)")
    ).strip() or "(no reading history yet)"

    prompt = (
        f"Based on the reader's reading history below, generate a concise interest profile "
        f"for use in article relevance scoring.\n\n"
        f"Format the output as exactly three lines:\n"
        f"High relevance: [topics the reader is most interested in]\n"
        f"Moderate relevance: [topics of occasional interest]\n"
        f"Avoid: [topics or content types the reader has no interest in]\n\n"
        f"Be specific — use concrete topics, not vague categories. "
        f"Output only the three lines, no explanation.\n\n"
        f"---\n"
        f"{data}"
    )
    text, _, _ = await _complete(prompt, client, provider, model, max_tokens=400)
    return text


# ── internal ──────────────────────────────────────────────────────────────────

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
        return resp.content[0].text.strip(), resp.usage.input_tokens, resp.usage.output_tokens
    elif provider == "openai":
        resp = await client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content.strip(), resp.usage.prompt_tokens, resp.usage.completion_tokens
    elif provider == "gemini":
        resp = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
        )
        meta = resp.usage_metadata
        return (
            resp.text.strip(),
            getattr(meta, "prompt_token_count", 0) or 0,
            getattr(meta, "candidates_token_count", 0) or 0,
        )
    raise ValueError(f"Unknown provider: {provider}")


# ── cost estimation ───────────────────────────────────────────────────────────

_MIN_JOBS_FOR_ACTUAL_AVG = 5


async def estimate_monthly_cost(user_id: int, db: AsyncSession) -> dict:
    """
    Estimate monthly AI cost.
    Uses actual avg tokens from article_ai_jobs (last 30 days) when enough data exists,
    otherwise falls back to fixed constants.
    """
    from sqlalchemy import text
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)

    # Labeled articles in last 30 days (scoring volume)
    labeled_result = await db.execute(
        text("""
            SELECT COUNT(DISTINCT a.id) FROM articles a
            JOIN article_labels al ON al.article_id = a.id
            JOIN user_article_states uas ON uas.article_id = a.id AND uas.user_id = :uid
            WHERE a.fetched_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    labeled_count = int(labeled_result.scalar() or 0)

    # Starred articles in last 30 days (summary volume)
    starred_result = await db.execute(
        text("""
            SELECT COUNT(*) FROM user_article_states
            WHERE user_id = :uid AND is_starred = true AND created_at >= :cutoff
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    starred_count = int(starred_result.scalar() or 0)

    # Actual avg tokens from completed scoring jobs
    scoring_jobs_result = await db.execute(
        text("""
            SELECT COUNT(*), AVG(input_tokens + COALESCE(output_tokens, 0))
            FROM article_ai_jobs
            WHERE user_id = :uid AND operation = 'scoring' AND status = 'success'
              AND processed_at >= :cutoff AND input_tokens IS NOT NULL
        """),
        {"uid": user_id, "cutoff": cutoff},
    )
    sj_row = scoring_jobs_result.one()
    scoring_job_count = int(sj_row[0] or 0)
    scoring_avg_tokens = float(sj_row[1] or 0)

    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    fast_model = s.ai_fast_model if s else None
    quality_model = s.ai_quality_model if s else None

    if scoring_job_count >= _MIN_JOBS_FOR_ACTUAL_AVG:
        scoring_tokens = int(scoring_avg_tokens * labeled_count)
        scoring_data_note = f"Based on {scoring_job_count} jobs in last 30 days"
    else:
        scoring_tokens = labeled_count * _SCORING_TOKENS_PER_ARTICLE
        scoring_data_note = "Estimated (not enough data yet)" if scoring_job_count > 0 else "Estimated"

    summary_tokens = starred_count * _SUMMARY_TOKENS_PER_ARTICLE

    return {
        "scoring": {
            "articles": labeled_count,
            "tokens": scoring_tokens,
            "cost": estimate_cost_usd(fast_model, scoring_tokens) if fast_model else None,
            "model": fast_model,
            "data_note": scoring_data_note,
        },
        "summary": {
            "articles": starred_count,
            "tokens": summary_tokens,
            "cost": estimate_cost_usd(quality_model, summary_tokens) if quality_model else None,
            "model": quality_model,
            "data_note": "Estimated",
        },
    }

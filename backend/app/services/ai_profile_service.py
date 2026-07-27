"""Automatic regeneration of the interest profile used by AI scoring.

The profile itself is produced by ``ai_service.generate_preference_text``; this
module decides *when* it is worth regenerating and guards what gets stored:

- generating from nearly unchanged reading data only reshuffles the wording and
  destabilises scoring, so a run needs a minimum of fresh engagement signals;
- below the cold-start threshold the generator pads the profile with feed names,
  which must never silently overwrite a good profile;
- output that does not look like a profile is rejected instead of saved, and a
  paid attempt (successful or not) starts a full-interval cooldown so a broken
  key cannot burn one call a day.
"""
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai import UserAiKey
from app.models.article import AiUsageLog
from app.models.user import UserSettings
from app.services.ai_service import generate_preference_text, get_ai_client

logger = logging.getLogger(__name__)

# Reading signals needed before the generator stops padding with feed names.
# Same number the settings page shows as "strong reading signals".
MIN_STRONG_SIGNALS = 20
# Fresh engagement needed since the profile last changed, or the run is skipped.
MIN_NEW_SIGNALS = 20
# Values accepted by the settings form; 0 = automatic generation off.
AUTO_INTERVALS = (0, 14, 28)
MAX_CONSECUTIVE_FAILS = 3

# Windows used by the generator's own signal groups (ai_service.generate_preference_text).
_STRONG_WINDOW_DAYS = 180
_READ_WINDOW_DAYS = 120

_MIN_LEN = 40
_MAX_LEN = 5000  # matches the settings form limit
_MIN_LINES = 2
_MAX_LINES = 6
_MAX_LINE_LEN = 1000
_REFUSAL_RE = re.compile(r"^(i'm sorry|i am sorry|i cannot|i can't|as an ai)", re.IGNORECASE)


def normalize_preference_text(raw: str | None) -> tuple[str | None, str | None]:
    """Validate generated profile text, returning (clean text, rejection reason).

    Deliberately structural rather than literal: the prompt asks for
    ``High relevance:`` / ``Moderate relevance:`` / ``Avoid:``, but models wrap
    labels in markdown, change the capitalisation, or answer in the language of
    the articles, and a literal check would block the profile forever.

    Only the label lines are kept, so a preamble the model added around the
    format cannot reach the scoring prompt, where the profile is interpolated
    verbatim (``ai_service.score_article``).
    """
    stripped = (raw or "").strip()
    if not stripped:
        return None, "empty response"
    if _REFUSAL_RE.match(stripped):
        return None, "model declined to answer"

    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    if len(lines) > _MAX_LINES:
        return None, f"expected at most {_MAX_LINES} lines, got {len(lines)}"
    labelled = [ln for ln in lines if ":" in ln]
    if len(labelled) < _MIN_LINES:
        return None, "no recognisable 'label: topics' lines"
    if not any(ln.split(":", 1)[1].strip() for ln in labelled):
        return None, "all topic lines are empty"
    if any(len(ln) > _MAX_LINE_LEN for ln in labelled):
        return None, "a line is implausibly long"

    cleaned = "\n".join(labelled)
    if len(cleaned) < _MIN_LEN:
        return None, f"too short ({len(cleaned)} characters)"
    if len(cleaned) > _MAX_LEN:
        return None, f"too long ({len(cleaned)} characters)"
    return cleaned, None


async def signal_counts(user_id: int, since: datetime | None, db: AsyncSession) -> tuple[int, int]:
    """Return (strong signals, new signals since ``since``) in one pass.

    Strong signals mirror groups G1+G2 of the generator (and
    ``get_preference_strong_count``). New signals count engagement whose most
    recent timestamp is newer than ``since``; ``user_article_states`` has no
    ``updated_at``, so GREATEST over the timestamps it does have is the closest
    approximation. ``is_read`` is never a signal — mark-all-read would fake it.
    """
    now = datetime.now(timezone.utc)
    row = (await db.execute(text("""
        SELECT
          COUNT(*) FILTER (
            WHERE user_starred AND (dwell_seconds >= 60 OR link_opened)
          ) AS strong_starred,
          COUNT(*) FILTER (
            WHERE NOT user_starred AND (dwell_seconds >= 60 OR link_opened)
              AND created_at >= :read_cutoff
          ) AS strong_read,
          COUNT(*) FILTER (
            WHERE (user_starred OR dwell_seconds >= 60 OR link_opened)
              AND GREATEST(
                    COALESCE(starred_at, created_at),
                    COALESCE(read_at, created_at),
                    created_at
                  ) > :since
          ) AS fresh
        FROM user_article_states
        WHERE user_id = :uid AND created_at >= :strong_cutoff
    """), {
        "uid": user_id,
        "strong_cutoff": now - timedelta(days=_STRONG_WINDOW_DAYS),
        "read_cutoff": now - timedelta(days=_READ_WINDOW_DAYS),
        "since": since or datetime(1970, 1, 1, tzinfo=timezone.utc),
    })).one()
    return int(row[0] or 0) + int(row[1] or 0), int(row[2] or 0)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def preference_auto_status(
    settings: UserSettings, db: AsyncSession, now: datetime | None = None
) -> tuple[str, dict]:
    """Single source of truth for "should the profile be regenerated, and if not, why".

    Used by both the scheduler job and the settings page, so the status line can
    never claim something other than what the job actually does. Returns one of
    ``off`` / ``up_to_date`` / ``cooldown`` / ``no_quality_model`` / ``no_api_key`` /
    ``cold_start`` / ``not_enough_new`` / ``due`` plus details for the UI.
    """
    now = now or datetime.now(timezone.utc)
    interval = settings.ai_preference_auto_days or 0
    if interval <= 0:
        return "off", {}

    delta = timedelta(days=interval)
    updated_at = _as_utc(settings.ai_preference_updated_at)
    if updated_at is not None and now - updated_at < delta:
        return "up_to_date", {"next_at": updated_at + delta}

    last_attempt = _as_utc(settings.ai_preference_last_attempt_at)
    if last_attempt is not None and now - last_attempt < delta:
        return "cooldown", {"next_at": last_attempt + delta}

    if not settings.ai_quality_provider or not settings.ai_quality_model:
        return "no_quality_model", {}

    # A configured model the job cannot use is still a reason it will not run:
    # get_ai_client returns nothing without a key, and the job skips. Checking only
    # for the stored row, not decrypting it, keeps a settings page render out of the
    # plaintext-key business (a key that exists but cannot be decrypted is an instance
    # fault, logged by get_api_key, not a state the user can fix here).
    has_key = await db.scalar(
        select(UserAiKey.provider).where(
            UserAiKey.user_id == settings.user_id,
            UserAiKey.provider == settings.ai_quality_provider,
        )
    )
    if has_key is None:
        return "no_api_key", {"provider": settings.ai_quality_provider}

    strong, fresh = await signal_counts(settings.user_id, updated_at, db)
    if strong < MIN_STRONG_SIGNALS:
        return "cold_start", {"strong": strong, "needed": MIN_STRONG_SIGNALS}
    if fresh < MIN_NEW_SIGNALS:
        return "not_enough_new", {"missing": MIN_NEW_SIGNALS - fresh}
    return "due", {}


def _apply_failure(settings: UserSettings, message: str, now: datetime) -> None:
    """Record a failed attempt; disable automatic generation after repeated failures.

    ``ai_preference_last_error`` is the field the UI and admin dashboard read —
    ``last_ai_error`` is set as well (it drives the header badge) but any
    successful scoring or summary job clears that one within minutes.
    """
    settings.ai_preference_fail_count = (settings.ai_preference_fail_count or 0) + 1
    if settings.ai_preference_fail_count >= MAX_CONSECUTIVE_FAILS:
        settings.ai_preference_auto_days = 0
        message = f"{message} — automatic generation turned off after {MAX_CONSECUTIVE_FAILS} failures."
    settings.ai_preference_last_error = message[:500]
    settings.ai_preference_last_error_at = now
    settings.last_ai_error = f"Interest profile: {message}"[:500]
    settings.last_ai_error_at = now


async def run_auto_generation(user_id: int, db: AsyncSession) -> str:
    """Regenerate one user's interest profile if it is due. Commits its own work.

    Returns ``generated`` / ``skipped:<status>`` / ``failed:<reason>`` for the
    job log. Never raises: one user's broken key must not stop the batch.
    """
    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if settings is None:
        return "skipped:off"

    now = datetime.now(timezone.utc)
    status, _detail = await preference_auto_status(settings, db, now)
    if status != "due":
        return f"skipped:{status}"

    client, provider, model = await get_ai_client(user_id, "quality", db)
    if client is None:
        # Provider/model are set but the key is missing: configuration, not failure.
        return "skipped:no_quality_model"

    try:
        raw, in_tok, out_tok = await generate_preference_text(user_id, db, client, provider, model)
    except Exception as exc:
        logger.warning("Auto profile generation failed for user=%s: %s", user_id, exc)
        settings.ai_preference_last_attempt_at = now
        _apply_failure(settings, str(exc), now)
        await db.commit()
        return "failed:error"

    # Tokens are spent whether or not the output survives validation.
    settings.ai_preference_last_attempt_at = now
    db.add(AiUsageLog(
        user_id=user_id,
        operation="preference_generation",
        model_slot="quality",
        model=model,
        provider=provider,
        input_tokens=in_tok,
        output_tokens=out_tok,
    ))

    cleaned, reason = normalize_preference_text(raw)
    if cleaned is None:
        logger.warning("Auto profile output rejected for user=%s: %s", user_id, reason)
        _apply_failure(settings, f"generated profile rejected ({reason})", now)
        await db.commit()
        return "failed:invalid_output"

    settings.ai_preference_prev_text = settings.ai_preference_text
    settings.ai_preference_text = cleaned
    settings.ai_preference_updated_at = now
    settings.ai_preference_source = "auto"
    settings.ai_preference_fail_count = 0
    settings.ai_preference_last_error = None
    settings.ai_preference_last_error_at = None
    await db.commit()
    return "generated"

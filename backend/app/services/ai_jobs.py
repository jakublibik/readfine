"""Shared helpers for the AI job services (scoring + summary).

``ai_scoring_service`` and ``ai_summary_service`` are parallel pipelines: enqueue a
pending ``ArticleAiJob``, later execute it with a provider call, and on failure
apply the same retry/backoff policy. The identical parts live here so a change to
content normalization, the global kill-switch, HTTP-status extraction or the retry
policy happens once for both.
"""
import html as _html
import logging
import re
from datetime import datetime, timedelta

import nh3
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import ArticleAiJob
from app.models.settings import AppSettings
from app.models.user import AI_MIN_CHARS_MIN, UserSettings
from app.utils.url_validator import find_blocked_address

logger = logging.getLogger(__name__)

# Consecutive-failure retry policy shared by scoring and summary jobs (and reused
# for readable extraction's own retries): give up after MAX_RETRIES attempts, with
# the Nth backoff taken from BACKOFF_MINUTES (clamped to the last entry).
MAX_RETRIES = 3
BACKOFF_MINUTES = [5, 30, 120]

_WHITESPACE_RE = re.compile(r"\s+")

# The floor for AI a reader asked for by hand (the Summarize and Context buttons).
# Their own minimum length setting governs the automatic pipeline, where the cost
# multiplies by every article starred; a button someone presses on one article is
# a statement that this one is worth it, and refusing it leaves no way through
# except editing settings and coming back. All this floor rules out is a headline
# with no body behind it, which is nothing for a model to work from.
#
# Tied to the lowest value the setting can take rather than repeated as its own
# number: a floor above that lowest value would invert the whole arrangement, with
# the button refusing an article the automatic run summarizes on its own.
ON_DEMAND_MIN_CHARS = AI_MIN_CHARS_MIN


def normalize_text(title: str, content: str | None) -> str:
    """Strip HTML, collapse whitespace, prepend the title. Not truncated.

    Kept separate from ``normalize_content`` because "is this article long
    enough?" must not depend on the content limit: measuring the truncated text
    makes the same article pass one gate and fail another, and a threshold above
    the limit would reject everything.
    """
    plain = nh3.clean(content or "", tags=set())
    plain = _html.unescape(plain)
    plain = _WHITESPACE_RE.sub(" ", plain).strip()
    return f"{title}\n\n{plain}" if plain else title


def normalize_content(title: str, content: str | None, limit: int) -> str:
    """``normalize_text`` truncated to *limit* chars — what gets sent to a model."""
    return normalize_text(title, content)[:limit]


async def ai_enabled_globally(db: AsyncSession) -> bool:
    """True when the admin AI kill-switch (AppSettings.ai_enabled) is on."""
    return bool(await db.scalar(select(AppSettings.ai_enabled).where(AppSettings.id == 1)))


def extract_http_status(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP status code from a provider exception."""
    for attr in ("status_code", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    m = re.search(r"\b([45]\d{2})\b", str(exc))
    return int(m.group(1)) if m else None


def apply_job_failure(
    job: ArticleAiJob,
    exc: Exception,
    now: datetime,
    *,
    operation: str,
    settings: "UserSettings | None",
) -> None:
    """Record a failed job attempt: bump the retry count, decide terminal-vs-backoff,
    and surface the error on the job and the user's ``last_ai_error`` banner.

    A permanent 4xx (client error other than 429) is terminal immediately; so is
    a refused address, and so is exhausting MAX_RETRIES; otherwise the job is
    rescheduled with a BACKOFF_MINUTES delay. *operation* ("scoring" / "summary")
    is used in the log line and the banner prefix.

    A refused address is terminal for the same reason a 4xx is: no amount of
    waiting turns an endpoint the instance is not allowed to reach into one it
    is. Retrying it would leave the reader watching a spinner for the length of
    the whole backoff before being told something a human has to fix. Its message
    replaces the provider's, which at that point is only "Connection error."
    """
    blocked = find_blocked_address(exc)
    msg = str(blocked or exc)[:300]
    http_status = extract_http_status(exc)
    retries = job.retry_count + 1
    job.retry_count = retries

    if blocked is not None:
        job.status = "failed"
        job.processed_at = now
    elif http_status is not None and 400 <= http_status < 500 and http_status != 429:
        job.status = "failed"
        job.processed_at = now
    elif retries >= MAX_RETRIES:
        job.status = "failed"
        job.processed_at = now
    else:
        delay = BACKOFF_MINUTES[min(retries - 1, len(BACKOFF_MINUTES) - 1)]
        job.next_retry_at = now + timedelta(minutes=delay)

    job.error_message = msg
    logger.warning(
        "AI %s failed job=%d article=%d user=%d: %s",
        operation, job.id, job.article_id, job.user_id, msg,
    )
    if settings is not None:
        settings.last_ai_error = f"{operation.capitalize()} error: {msg}"
        settings.last_ai_error_at = now
        settings.last_ai_error_article_id = job.article_id


def clear_last_ai_error(settings: "UserSettings") -> None:
    """Drop the error banner after a job succeeds, article link included."""
    settings.last_ai_error = None
    settings.last_ai_error_at = None
    settings.last_ai_error_article_id = None

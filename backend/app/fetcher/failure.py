"""What a failed fetch does to the feed row.

Two tiers, because two very different things get reported as a failed HTTP request:

* **Error tier** — something is wrong with the feed or the network (timeout, DNS,
  unparseable body, 404, 500). ``fetch_error_count`` climbs, the feed goes to
  ``error``, the scheduler backs it off, and after
  ``FETCH_ERROR_DISABLE_THRESHOLD`` consecutive failures it is disabled.
* **Block tier** — the host is refusing automated clients (anti-bot 403, bare 429).
  ``block_count`` climbs instead, the feed's status is left alone, and it is
  disabled only after ``BLOCK_DISABLE_THRESHOLD`` consecutive blocks.

The split exists because measurement showed blocks carry no information about the
feed. Reddit refuses ~34 % of requests at 45-minute spacing and ~18 % at 75-second
spacing, in waves lasting minutes to hours that hit every feed on the host at once.
Counting that as feed error disabled healthy feeds after five bad rounds.

Both counters are consecutive: any successful fetch resets both to zero.

Also home to :func:`arm_host_cooldown`, the other half of "what a failed fetch
does" — it writes no columns, it paces sibling feeds on the same host.
"""
from datetime import datetime, timedelta

import httpx
from sqlalchemy import case, literal
from sqlalchemy.sql.elements import ColumnElement

from app.fetcher import host_throttle
from app.models.feed import Feed
from app.utils.url_validator import (
    RETRYABLE_HTTP_STATUSES,
    TRANSIENT_HTTP_STATUSES,
    is_bot_block,
    parse_retry_after,
    rate_limited_until,
)

# Consecutive failures before a feed is disabled. Both are compared against the
# pre-increment column value inside the same UPDATE (see below), so the feed is
# actually disabled on failure N+1. Do not "fix" one without the other — the
# existing fetch_error_count tests encode that offset.
FETCH_ERROR_DISABLE_THRESHOLD = 5
BLOCK_DISABLE_THRESHOLD = 10

# Consecutive blocks before the UI says anything. A single 403 is noise on a feed
# that is otherwise fetching fine (at Reddit's measured rate, three in a row happens
# by chance in ~4 % of rounds), and an amber badge on a healthy feed is exactly the
# false alarm this change set out to remove.
BLOCK_BADGE_THRESHOLD = 3

# Blocked feeds keep status 'active', so they lose the scheduler's error backoff.
# Without a replacement they would retry on their normal interval and, across the
# three-hour wave measured on the VPS, hammer a refusing host ~8x harder than today.
# Applied through retry_after_until, which the scheduler already honors.
BLOCK_BACKOFF_BASE = timedelta(minutes=15)
BLOCK_BACKOFF_MAX = timedelta(hours=24)


def block_backoff(block_count: int) -> timedelta:
    """Exponential backoff for a blocked feed: 15, 30, 60, 120 min … capped at 24 h.

    *block_count* is the pre-increment value, so the first block yields the base.
    """
    if block_count >= 32:  # guard the shift; the cap has long since applied
        return BLOCK_BACKOFF_MAX
    return min(BLOCK_BACKOFF_BASE * 2 ** max(block_count, 0), BLOCK_BACKOFF_MAX)


def classify(exc: Exception) -> tuple[int | None, bool]:
    """Return ``(http_status, is_block)`` for a failed fetch.

    Only ``httpx.HTTPStatusError`` carries a response, and the caller's ``except``
    is broad (timeouts, DNS, SSRF validation, feedparser errors all land there), so
    the response is never touched without that guard.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None, False
    status = exc.response.status_code
    return status, is_bot_block(status, exc.response.headers)


def arm_host_cooldown(feed_url: str, exc: Exception, http_status: int | None, now: datetime) -> None:
    """Arm the in-memory per-host cooldowns from a failed response's headers.

    Paces sibling feeds on the same host; writes nothing to the feed row.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return
    host = host_throttle.host_key(feed_url)
    signal = rate_limited_until(exc.response.headers, now)

    if http_status in TRANSIENT_HTTP_STATUSES:
        # Arm the host-wide cooldown from any rate-limit headers (Reddit's 429 carries
        # x-ratelimit-reset but no Retry-After) so sibling feeds on the same host defer
        # instead of hammering into another 429.
        host_throttle.note_rate_limited(host, signal)
        if http_status == 429:
            # Ratchet the learned spacing tighter (429 only — 408 is a timeout, not a
            # throttle). Feed it *only* a real Retry-After: that is an instruction we can
            # act on. The reset-derived instant in `signal` works as a deadline but is the
            # phase left in the current window, not a rate — ratcheting on it is how
            # reddit.com ended up with a learned 78s that no measurement supported.
            asked = parse_retry_after(exc.response.headers.get("retry-after"), now)
            retry_after_s = (asked - now).total_seconds() if asked else None
            host_throttle.record_rate_limited(host, now, retry_after_s)
    elif http_status == 403:
        # A 403 with a real rate-limit signal is a genuine throttle → arm the rate-limit
        # cooldown (gates the scheduler *and* manual refreshes). A bare anti-bot 403 (no
        # usable timing) gets only the fixed breather, which paces the scheduler but never
        # blocks a manual refresh — the user asked to retry and the block is often
        # transient. The feed's own backoff comes from the block tier either way.
        if signal is not None:
            host_throttle.note_rate_limited(host, signal)
        else:
            host_throttle.note_block(host, now + host_throttle.FALLBACK_BLOCK_COOLDOWN)


def failure_values(exc: Exception, *, feed_block_count: int, now: datetime) -> dict:
    """Column values for the ``feeds`` UPDATE after a failed fetch.

    *feed_block_count* is the feed's current (pre-increment) block count, used only
    to size the backoff; the counter itself is incremented in SQL so concurrent
    fetches cannot lose an increment.
    """
    http_status, is_block = classify(exc)
    message = str(exc)[:500]

    if is_block:
        # A host-level refusal: leave status alone (never improve it, never worsen
        # it) unless we've been refused long enough that the feed looks genuinely
        # gone. A real Retry-After, if the host sent one, wins when it is longer.
        status: ColumnElement | str = case(
            (Feed.block_count >= BLOCK_DISABLE_THRESHOLD, literal("disabled")),
            else_=Feed.status,
        )
        retry_after_until = now + block_backoff(feed_block_count)
        if isinstance(exc, httpx.HTTPStatusError):
            asked = parse_retry_after(exc.response.headers.get("retry-after"), now)
            if asked is not None and asked > retry_after_until:
                retry_after_until = asked
        return {
            "status": status,
            "block_count": Feed.block_count + 1,
            "last_error": message,
            "last_fetched_at": now,
            "retry_after_until": retry_after_until,
        }

    # Error tier. 4xx other than the retryable ones is a permanent verdict on the
    # URL (404, 410, 451 …) — no point backing off, disable straight away.
    is_permanent_4xx = (
        http_status is not None
        and 400 <= http_status < 500
        and http_status not in RETRYABLE_HTTP_STATUSES
    )
    if is_permanent_4xx:
        status = literal("disabled")
    else:
        status = case(
            (Feed.fetch_error_count >= FETCH_ERROR_DISABLE_THRESHOLD, literal("disabled")),
            else_=literal("error"),
        )

    retry_after_until = None
    if http_status in TRANSIENT_HTTP_STATUSES and isinstance(exc, httpx.HTTPStatusError):
        retry_after_until = parse_retry_after(exc.response.headers.get("retry-after"), now)

    return {
        "status": status,
        "fetch_error_count": Feed.fetch_error_count + 1,
        "last_error": message,
        "last_fetched_at": now,
        "retry_after_until": retry_after_until,
    }

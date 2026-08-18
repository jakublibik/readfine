"""What a failed fetch does to the feed row.

Two tiers, because two very different things get reported as a failed HTTP request:

* **Error tier** — something is wrong with the feed or the network (timeout, DNS,
  unparseable body, 404, 500). ``fetch_error_count`` climbs, the feed goes to
  ``error``, the scheduler backs it off, and after
  ``FETCH_ERROR_DISABLE_THRESHOLD`` consecutive failures it is disabled. A 404
  uses the same counter but a shorter threshold of its own
  (``NOT_FOUND_DISABLE_THRESHOLD``).
* **Block tier** — the host is refusing automated clients (anti-bot 403, bare 429).
  ``block_count`` climbs instead, the feed's status is left alone, and it is
  disabled only after ``BLOCK_DISABLE_THRESHOLD`` consecutive blocks.

The split exists because measurement showed blocks carry no information about the
feed. Reddit refuses ~34 % of requests at 45-minute spacing and ~18 % at 75-second
spacing, in waves lasting minutes to hours that hit every feed on the host at once.
Counting that as feed error disabled healthy feeds after five bad rounds.

Both counters are consecutive: any successful fetch resets both to zero. When a fetch
never gets that far, :func:`clear_failure_state` is the manual way back — the one place
that undoes every column a failure writes.

A third case sits outside both tiers: a fault on *our* side (a broken query, a
column the code expects before its migration ran) also arrives as an exception in
the fetcher's broad ``except``. It says nothing about the feed, so it moves no
counter and changes no status, and its text stays out of the user's feed row — see
:func:`is_source_error` and :func:`user_failure_message`.

Also home to :func:`arm_host_cooldown`, the other half of "what a failed fetch
does" — it writes no columns, it paces sibling feeds on the same host.
"""
import re
from datetime import datetime, timedelta

import httpx
from sqlalchemy import case, literal
from sqlalchemy.sql.elements import ColumnElement

from app.fetcher import host_throttle
from app.models.feed import Feed
from app.utils.url_validator import (
    RETRYABLE_HTTP_STATUSES,
    TRANSIENT_HTTP_STATUSES,
    ResponseTooLarge,
    is_bot_block,
    parse_retry_after,
    rate_limited_until,
    redact_url,
)

# Consecutive failures before a feed is disabled. Both are compared against the
# pre-increment column value inside the same UPDATE (see below), so the feed is
# actually disabled on failure N+1. Do not "fix" one without the other — the
# existing fetch_error_count tests encode that offset.
FETCH_ERROR_DISABLE_THRESHOLD = 5
BLOCK_DISABLE_THRESHOLD = 10

# Consecutive 404s before a feed is disabled. Unlike 410 and 451, a 404 is not a
# reliable verdict on the address: hosts serve it as a transient backend hiccup, in
# waves that take out every feed on the host at once. Observed on YouTube's
# feeds/videos.xml, which 404s for valid channel ids for a stretch and then serves
# them again — disabling on the first hit turned that into a dead feed that only a
# manual re-enable brought back. Deliberately lower than the general error
# threshold: a 404 is still weak evidence the feed is gone, just not proof.
NOT_FOUND_DISABLE_THRESHOLD = 4

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


# HTTP credentials written into an address (``//user:pass@host``). Matched on shape so
# an address that reaches the text by some other route is covered too, not only the
# feed's own; the userinfo is dropped rather than marked, which is what redact_url does.
_USERINFO_RE = re.compile(r"//[^/@\s]+:[^/@\s]*@")


def _redacted(text: str, feed_url: str) -> str:
    """Strip an address's secrets out of a message that quotes it.

    A message is stored in ``fetch_logs`` and ``Feed.last_error`` and shown in the
    admin dashboard and the user's feed list, next to a column that runs the address
    through ``redact_url_display``. Quoting the raw address in the message would walk
    straight around that redaction, so the same secrets come out here: the whole query
    string (a feed address legitimately carries an API key in it) when the message
    quotes the feed's address, and embedded credentials wherever they appear.
    """
    if feed_url and feed_url in text:
        text = text.replace(feed_url, redact_url(feed_url))
    return _USERINFO_RE.sub("//", text)


# Exception types that mean the *source* failed: an HTTP status, a timeout, DNS, TLS,
# too large a body, a body that will not parse, an address the SSRF validator turned
# down. Anything else the fetch can raise is a fault on our side. Deliberately an
# allowlist rather than a list of our own failure modes, so a way of breaking that
# nobody anticipated reads as internal, which is the safe way round.
#
# ValueError is in here because it is how this path reports source problems: "Not a
# valid RSS/Atom feed" (rss.py), "Cannot resolve hostname" and "Redirect blocked"
# (url_validator.py), a CSS selector matching no links (scrape.py). That is broad
# enough that a ValueError raised by a bug of ours goes out to the user too; its text
# is uninformative rather than revealing, and narrowing it would mean a fetch-specific
# exception type threaded through the shared, SSRF-safe fetch layer.
_SOURCE_ERRORS = (httpx.HTTPError, ValueError, ResponseTooLarge)

# What the user's feed row says when the fault was ours. Deliberately says nothing
# about what broke: the full text is in fetch_logs, which only an admin can read.
INTERNAL_ERROR_MESSAGE = "Internal error while fetching this feed. The detail is in the fetch log."


def is_source_error(exc: Exception) -> bool:
    """Did the feed's source fail, as opposed to something on our side?

    The fetcher's ``except`` is broad enough to catch our own bugs (a broken query,
    a column read before its migration ran), and those must not be reported as the
    feed's fault, in the row or in the counters.
    """
    return isinstance(exc, _SOURCE_ERRORS)


def log_failure_message(exc: Exception, feed_url: str) -> str:
    """Full text of a failed fetch, for ``fetch_logs`` (admin-only).

    For an HTTP status error, ``str(exc)`` embeds httpx's request URL, whose host
    is the validated IP the connection was pinned to (see ``_pin_connection``) — an
    ephemeral address that means nothing to the admin reading the row. Rebuild the
    line from the status, its reason phrase and the feed's own (redacted) URL. Every
    other failure (timeout, DNS, feedparser, SSRF, and our own bugs) never carried an
    IP, so keep its original text, minus anything secret it quoted (see
    :func:`_redacted`).
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        reason = exc.response.reason_phrase
        label = f"HTTP {status} {reason}".rstrip()
        return f"{label}: {redact_url(feed_url)}"[:500]
    return _redacted(str(exc), feed_url)[:500]


def user_failure_message(exc: Exception, feed_url: str) -> str:
    """``Feed.last_error`` for a failed fetch, as the subscriber will read it.

    Unlike :func:`log_failure_message`, this one is shown outside the admin panel:
    the feed list and the feed's edit form in Settings, and the error strip above the
    article list. A source failure goes out verbatim, since the subscriber is the one
    who can act on it. A fault of ours would put our internals in front of someone
    who can do nothing with them — an unrun migration used to send the failing SELECT
    and its column names out this way — so it becomes one flat sentence, with the
    detail left in ``fetch_logs``.
    """
    if not is_source_error(exc):
        return INTERNAL_ERROR_MESSAGE
    return log_failure_message(exc, feed_url)


def has_failure_trail(feed: Feed) -> bool:
    """Is there anything on this feed row that a failed fetch put there?

    The condition for offering a "reset errors" action. Deliberately covers
    ``retry_after_until`` on its own: a feed refused by the host keeps status
    ``active`` with both counters below their badge thresholds for the first couple
    of rounds, and the only visible consequence is a fetch deferred by hours.

    The status is *not* part of it. Every automatic stop leaves a message and a counter
    behind, so a bare ``disabled`` with a clean row is somebody switching the feed off by
    hand — offering to "reset errors" there would mean an admin undoing their own
    decision with a button that claims to do something else.
    """
    return bool(
        feed.last_error
        or feed.fetch_error_count
        or feed.block_count
        or feed.retry_after_until is not None
    )


def clear_failure_state(feed: Feed) -> None:
    """Wipe everything a failed fetch left on the feed row — the counterpart to
    :func:`failure_values`, and the only supported way back from a stopped feed.

    Lives here so the two halves cannot drift: a new column written by a failure has
    to be undone by something, and this is the one place that does it. Every deliberate
    revival goes through it — saving the feed's edit form, the admin's "Reset errors",
    an admin switching a feed back to active, setting credentials over the API — because
    each of those used to clear a different subset. Clearing only ``fetch_error_count``
    is what left feeds sitting at "throttled x14" that no button could bring back.

    ``retry_after_until`` goes too, including a deadline the host itself asked for. It is
    the whole point of the action: the block tier's own backoff reaches 24 h, so leaving
    it would mean the feed is revived on paper and still not fetched until tomorrow, by
    the scheduler *or* by a manual refresh (see :func:`app.fetcher.rss.cooldown_until` —
    once the block count is zeroed, that deadline starts gating manual fetches too). The
    host's instruction is not lost with it: ``arm_host_cooldown`` armed the same deadline
    on the per-host throttle, which gates the scheduler and manual refreshes alike. That
    one is in-memory, so a restart in between costs at most one request that gets refused
    again and re-arms both.

    ``paused`` is left alone — that is somebody's decision, not a failure.
    """
    if feed.status in ("error", "disabled"):
        feed.status = "active"
    feed.fetch_error_count = 0
    feed.block_count = 0
    feed.last_error = None
    feed.retry_after_until = None


def failure_values(exc: Exception, *, feed_url: str, feed_block_count: int, now: datetime) -> dict:
    """Column values for the ``feeds`` UPDATE after a failed fetch.

    *feed_block_count* is the feed's current (pre-increment) block count, used only
    to size the backoff; the counter itself is incremented in SQL so concurrent
    fetches cannot lose an increment.
    """
    http_status, is_block = classify(exc)
    message = user_failure_message(exc, feed_url)

    if not is_source_error(exc):
        # Our own fault, so no verdict on the feed: record what happened and that we
        # tried, and leave the counters, the status and any deadline the host asked
        # for exactly as they were. Without this, the window between deploying code
        # and its migration running would take healthy feeds down for good — every
        # feed failing every round, five rounds to the disable threshold, and a
        # manual re-enable each to bring them back. last_fetched_at is still written,
        # or the scheduler would retry the same broken round on every pass.
        return {"last_error": message, "last_fetched_at": now}

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
    # URL (410, 451 …) — no point backing off, disable straight away. 404 is the
    # exception: it goes through the counter on its own, shorter threshold, because
    # hosts also use it for transient failures (see NOT_FOUND_DISABLE_THRESHOLD).
    is_permanent_4xx = (
        http_status is not None
        and 400 <= http_status < 500
        and http_status != 404
        and http_status not in RETRYABLE_HTTP_STATUSES
    )
    if is_permanent_4xx:
        status = literal("disabled")
    else:
        threshold = (
            NOT_FOUND_DISABLE_THRESHOLD if http_status == 404
            else FETCH_ERROR_DISABLE_THRESHOLD
        )
        status = case(
            (Feed.fetch_error_count >= threshold, literal("disabled")),
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

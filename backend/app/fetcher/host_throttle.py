"""In-memory per-host fetch cooldowns.

Two layers, because they gate different callers:

* **Rate-limit cooldown** (``note_rate_limited``) — armed from a real signal a host
  gave us (HTTP 429 / ``Retry-After`` / ``RateLimit-*``). Retrying into it is
  genuinely pointless, so it gates *everyone*: the scheduler and manual refreshes.
* **Block breather** (``note_block``) — a fixed fallback armed on a bare HTTP 403
  anti-bot block that carried no usable timing. It only paces the *scheduler* (and
  sibling feeds on the host) so background fetching stops hammering into the block;
  it deliberately does **not** block a manual refresh, where the user explicitly
  asked to retry and the 403 may well be transient.

State is process-local and non-persistent — matching the in-process APScheduler
deployment. A restart simply clears cooldowns; at most one probe request per host
gets re-blocked, which re-arms the cooldown. Not safe across multiple scheduler
processes (the deployment runs a single in-process scheduler).
"""
from datetime import datetime, timedelta
from urllib.parse import urlparse

# Fallback cooldown for a host that blocked us with HTTP 403 but gave no usable
# rate-limit timing (no RateLimit-* headers, or a useless ``Retry-After: 0`` —
# both typical of Reddit/YouTube anti-bot blocks). See module docstring for why
# this only paces the scheduler and never blocks a manual refresh.
FALLBACK_BLOCK_COOLDOWN = timedelta(seconds=60)

# host -> UTC instant before which we should not fetch that host again.
_cooldown: dict[str, datetime] = {}        # rate-limit cooldown (gates everyone)
_block_cooldown: dict[str, datetime] = {}  # 403 breather (gates the scheduler only)


def host_key(url: str) -> str:
    """Normalize a feed URL to a host key for per-host throttling.

    Lower-cased hostname with a leading ``www.`` stripped, so ``www.reddit.com``
    and ``reddit.com`` share one throttle. Falls back to the raw URL when there is
    no parseable host.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or url


def _note(store: dict[str, datetime], host: str, until: datetime | None) -> None:
    """Record a cooldown for *host* in *store*, keeping the later of any existing
    entry. No-op when *until* is None."""
    if until is None:
        return
    existing = store.get(host)
    if existing is None or until > existing:
        store[host] = until


def _peek(store: dict[str, datetime], host: str, now: datetime) -> datetime | None:
    """Return the cooldown expiry from *store* if still active, else None (dropping
    an expired entry)."""
    until = store.get(host)
    if until is None:
        return None
    if until <= now:
        del store[host]
        return None
    return until


def note_rate_limited(host: str, until: datetime | None) -> None:
    """Arm the rate-limit cooldown (real 429/Retry-After signal). Gates everyone."""
    _note(_cooldown, host, until)


def note_block(host: str, until: datetime | None) -> None:
    """Arm the 403 anti-bot breather. Paces the scheduler only, not manual fetches."""
    _note(_block_cooldown, host, until)


def blocked_until(host: str, now: datetime, *, include_block: bool = False) -> datetime | None:
    """Return the cooldown expiry if *host* is still cooling down, else None.

    Always considers the rate-limit cooldown. Set ``include_block=True`` (the
    scheduler) to also honor the 403 breather; manual refreshes leave it False so a
    bare-403 breather never blocks an explicit retry. Returns the later of the
    considered cooldowns; drops expired entries as a side effect.
    """
    candidates = []
    rate_limit = _peek(_cooldown, host, now)
    if rate_limit is not None:
        candidates.append(rate_limit)
    if include_block:
        block = _peek(_block_cooldown, host, now)
        if block is not None:
            candidates.append(block)
    return max(candidates) if candidates else None


def clear() -> None:
    """Drop all cooldowns (test helper)."""
    _cooldown.clear()
    _block_cooldown.clear()

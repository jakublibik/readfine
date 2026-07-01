"""In-memory per-host fetch cooldown.

When a host reports (via rate-limit response headers) that we've exhausted its
budget, we record when it next allows a request. The scheduler consults this to
defer same-host feeds instead of hammering into HTTP 429.

State is process-local and non-persistent — matching the in-process APScheduler
deployment. A restart simply clears cooldowns; at most one probe request per host
gets a 429, which re-arms the cooldown. Not safe across multiple scheduler
processes (the deployment runs a single in-process scheduler).
"""
from datetime import datetime
from urllib.parse import urlparse

# host -> UTC instant before which we should not fetch that host again.
_cooldown: dict[str, datetime] = {}


def _host_key(url: str) -> str:
    """Normalize a feed URL to a host key for per-host throttling.

    Lower-cased hostname with a leading ``www.`` stripped, so ``www.reddit.com``
    and ``reddit.com`` share one throttle. Falls back to the raw URL when there is
    no parseable host.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or url


def note_rate_limited(host: str, until: datetime | None) -> None:
    """Record that *host* is rate-limited until *until* (keeping the later of any
    existing cooldown). No-op when *until* is None."""
    if until is None:
        return
    existing = _cooldown.get(host)
    if existing is None or until > existing:
        _cooldown[host] = until


def blocked_until(host: str, now: datetime) -> datetime | None:
    """Return the cooldown expiry if *host* is still cooling down, else None
    (dropping an expired entry)."""
    until = _cooldown.get(host)
    if until is None:
        return None
    if until <= now:
        del _cooldown[host]
        return None
    return until


def clear() -> None:
    """Drop all cooldowns (test helper)."""
    _cooldown.clear()

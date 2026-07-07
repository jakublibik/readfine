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
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlparse

# Fallback cooldown for a host that blocked us with HTTP 403 but gave no usable
# rate-limit timing (no RateLimit-* headers, or a useless ``Retry-After: 0`` —
# both typical of Reddit/YouTube anti-bot blocks). See module docstring for why
# this only paces the scheduler and never blocks a manual refresh.
FALLBACK_BLOCK_COOLDOWN = timedelta(seconds=60)

# ── Learned per-host spacing (monotonic ratchet) ──────────────────────────────
# A minimum gap the scheduler leaves between same-host fetches, learned from what a
# host advertises. It learns *precisely* from live RateLimit-* headers on a success
# (reset/remaining) and *tightens* on repeated 429s; it never auto-loosens (that
# would oscillate around the limit). An admin clears/overrides it manually.
GLOBAL_MIN_SPACING = 2.0    # floor enforced for every host, incl. hosts we know nothing about (s)
MAX_SPACING = 600.0         # cap on a learned spacing so a feed never stalls forever (10 min)
SPACING_MARGIN = 1.15       # multiplicative tighten applied when a 429 ratchet fires
TIGHTEN_AFTER_429 = 2       # consecutive 429s before tightening (debounces a lone transient 429)


@dataclass
class LearnedSpacing:
    host: str
    seconds: float          # learned min gap; 0.0 means "tracking only, nothing learned yet"
    source: str             # "200" | "429" | "manual"
    learned_at: datetime
    consecutive_429: int = 0


# host -> UTC instant before which we should not fetch that host again.
_cooldown: dict[str, datetime] = {}        # rate-limit cooldown (gates everyone)
_block_cooldown: dict[str, datetime] = {}  # 403 breather (gates the scheduler only)
_spacing: dict[str, LearnedSpacing] = {}   # learned per-host min spacing
_dirty: set[str] = set()                   # hosts whose spacing changed since last DB flush


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


def record_success(host: str, now: datetime, spacing_seconds: float | None = None) -> LearnedSpacing | None:
    """Update the learned spacing from a successful (2xx/304) response.

    With usable ``RateLimit-*`` headers (*spacing_seconds* set) the value ratchets
    **up** to ``reset/remaining`` — never down, so a host loosening its limit doesn't
    make us re-probe into a 429. Any success also clears the 429 debounce streak.
    Returns the changed entry (for persistence) or ``None`` when nothing changed.
    """
    existing = _spacing.get(host)
    if spacing_seconds is None:
        # No advertised spacing: only meaningful effect is resetting a 429 streak.
        if existing is None or existing.consecutive_429 == 0:
            return None
        existing.consecutive_429 = 0
        existing.learned_at = now
        _dirty.add(host)
        return existing
    prev = existing.seconds if existing else 0.0
    seconds = min(max(prev, spacing_seconds), MAX_SPACING)
    entry = LearnedSpacing(host, seconds, "200", now, 0)
    _spacing[host] = entry
    _dirty.add(host)
    return entry


def record_rate_limited(host: str, now: datetime, retry_after_seconds: float | None = None) -> LearnedSpacing:
    """Ratchet the learned spacing on a 429. Tightens only after
    ``TIGHTEN_AFTER_429`` consecutive 429s (a lone transient 429 just bumps the
    streak). The 429 ``Retry-After`` is used only as a lower bound — it marks the
    window reset, not a per-request spacing — then multiplied by ``SPACING_MARGIN``.
    """
    existing = _spacing.get(host)
    prev = existing.seconds if existing else 0.0
    streak = (existing.consecutive_429 if existing else 0) + 1
    if streak < TIGHTEN_AFTER_429:
        entry = LearnedSpacing(host, prev, existing.source if existing else "429", now, streak)
    else:
        base = max(prev, retry_after_seconds or 0.0, GLOBAL_MIN_SPACING)
        seconds = min(base * SPACING_MARGIN, MAX_SPACING)
        entry = LearnedSpacing(host, seconds, "429", now, streak)
    _spacing[host] = entry
    _dirty.add(host)
    return entry


def effective_spacing(host: str) -> float:
    """Enforced min gap for *host*: the learned value floored at ``GLOBAL_MIN_SPACING``
    and capped at ``MAX_SPACING``. Hosts we know nothing about still get the floor."""
    existing = _spacing.get(host)
    learned = existing.seconds if existing else 0.0
    return min(max(learned, GLOBAL_MIN_SPACING), MAX_SPACING)


def arm_after_fetch(host: str, now: datetime) -> None:
    """Arm post-fetch pacing for *host* (called by the scheduler after each fetch).

    Two layers, deliberately gating different callers:

    * A scheduler-only breather at the *effective* spacing (learned value floored at
      ``GLOBAL_MIN_SPACING``) — flattens same-host bursts without touching manual refresh.
    * When a **real** limit has been learned (``seconds > 0``, from 200 headers or a 429
      ratchet), a cooldown that also holds off *manual* refreshes: forcing a fetch inside
      a host's known rate-limit gap just earns a 429, so the manual paths show
      "try again in X" instead. The bare ``GLOBAL_MIN_SPACING`` floor is never applied to
      manual — it's pacing, not a host requirement.
    """
    note_block(host, now + timedelta(seconds=effective_spacing(host)))
    entry = _spacing.get(host)
    if entry and entry.seconds > 0:
        note_rate_limited(host, now + timedelta(seconds=entry.seconds))


def set_manual_spacing(host: str, seconds: float, now: datetime) -> LearnedSpacing:
    """Admin override: pin a spacing (source ``manual``). Clamped to ``MAX_SPACING``."""
    entry = LearnedSpacing(host, min(max(seconds, 0.0), MAX_SPACING), "manual", now, 0)
    _spacing[host] = entry
    _dirty.add(host)
    return entry


def clear_spacing(host: str) -> bool:
    """Admin reset: forget the learned spacing for *host* so it re-learns. Returns
    True if an entry was removed."""
    removed = _spacing.pop(host, None) is not None
    if removed:
        _dirty.add(host)  # flush deletes the row
    return removed


def get_spacing(host: str) -> LearnedSpacing | None:
    """Current learned entry for *host*, or None."""
    return _spacing.get(host)


def all_spacing() -> list[LearnedSpacing]:
    """Snapshot of learned spacings (for the admin view), most-spaced first."""
    return sorted(_spacing.values(), key=lambda s: s.seconds, reverse=True)


def drain_dirty() -> set[str]:
    """Return and clear the set of hosts changed since the last flush (for DB write-back)."""
    hosts = set(_dirty)
    _dirty.clear()
    return hosts


def load_spacing(entries: list[LearnedSpacing]) -> None:
    """Replace the in-memory spacing store (used to hydrate from the DB at startup).
    Does not mark anything dirty — this is a load, not a change."""
    _spacing.clear()
    for e in entries:
        _spacing[e.host] = e
    _dirty.clear()


def clear() -> None:
    """Drop all cooldowns and learned spacing (test helper)."""
    _cooldown.clear()
    _block_cooldown.clear()
    _spacing.clear()
    _dirty.clear()

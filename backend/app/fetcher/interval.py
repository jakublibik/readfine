"""Adaptive fetch interval: derive a feed's poll cadence from its real publish rate.

Instead of a flat default, the scheduler can poll each feed on an interval derived
from how often it actually publishes (the Miniflux ``entry_frequency`` model): count
items in a trailing window, ``interval = window / count * FACTOR``, floored at
``AUTO_FLOOR``. The derived value is stored UNCAPPED and quantized to 15 min; the
per-read cap (``max_fetch_interval_min``) is applied at selection time, not baked in
here, so an admin changing the cap takes effect without a recompute.

This is unrelated to ``host_throttle`` (per-host rate-limit spacing) — that is a
politeness floor keyed by host, this is a per-feed cadence target.
"""
from datetime import datetime, timedelta

# Trailing window over which publish rate is measured.
WINDOW_DAYS = 7
WINDOW_MIN = WINDOW_DAYS * 24 * 60
# Poll ~25% more often than the feed publishes, so we rarely lag a full publish cycle.
FACTOR = 0.75
# Auto never polls faster than this. The global min_fetch_interval_min (default 15)
# still floors manual overrides; auto gets its own, higher floor.
AUTO_FLOOR = 30


def quantize15(minutes: float) -> int:
    """Round *minutes* to the nearest 15-min multiple, floored at 15.

    Unlike ``admin._quantize15`` there is no upper clamp: a derived interval for a
    slow feed is stored uncapped (the read-time cap handles the ceiling).
    """
    return max(15, round(minutes / 15) * 15)


def derive_interval_min(
    *,
    created_at: datetime,
    count: int,
    now: datetime,
    window_min: int = WINDOW_MIN,
) -> int | None:
    """Derived poll interval (minutes) for a feed, or ``None`` when there isn't yet a
    full window of history — a brand-new feed, where the caller falls back to the
    default. (This also skips the initial-subscription backfill burst.)

    *count* is the number of items with ``coalesce(published_at, fetched_at)`` inside
    the trailing window. ``count == 0`` (an established but quiet feed) collapses via
    ``max(count, 1)`` into a large value that the read-time cap trims — no special
    case. Returns a floored, UNCAPPED, 15-min-quantized value.
    """
    if created_at > now - timedelta(minutes=window_min):
        return None
    raw = window_min / max(count, 1) * FACTOR
    return quantize15(max(raw, AUTO_FLOOR))


def auto_interval_min(
    derived_interval_min: int | None,
    *,
    default_interval_min: int,
    min_interval_min: int,
    max_interval_min: int,
) -> int:
    """The interval the "Auto" (adaptive) mode resolves to for a feed, as a scalar.

    Single source of truth shared by the scheduler (``effective_interval_min`` and its
    SQL mirror) and the UI hints. A genuinely derived value is clamped to
    ``[min, max]`` — the read-time cap keeps a quiet feed from being polled too rarely.
    With no derived value yet the feed falls back to the admin's ``default_interval_min``,
    floored at the minimum but NOT capped: the default is an explicit baseline, so a
    default set above the cap is honoured until the feed has enough history.
    """
    if derived_interval_min is not None:
        return min(max(derived_interval_min, min_interval_min), max_interval_min)
    return max(default_interval_min, min_interval_min)

"""Shared server-side date/time formatting.

Single source of truth used by the Jinja ``localtime``/``utctime`` filters, the
article service (``formatted_date`` in the API) and briefings — so the relative
"today / this year / older" boundaries never drift between web, API and email.

The viewer's timezone is carried per-request via a ``ContextVar`` (set in the
auth dependency). This works across async, ``TemplateResponse`` and direct
``env.get_template(...).render(...)`` calls alike.
"""

from contextvars import ContextVar
from datetime import datetime, timezone
from functools import cache
import zoneinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Per-request viewer timezone (IANA name). Defaults to UTC when unauthenticated.
current_viewer_tz: ContextVar[str] = ContextVar("current_viewer_tz", default="UTC")

# Fixed month abbreviations — avoids dependence on the process locale (%b).
_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def resolve_tz(tz_str: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def format_local(
    dt: datetime | None,
    tz_str: str = "UTC",
    fmt: str = "short",
    now: datetime | None = None,
) -> str:
    """Format ``dt`` in ``tz_str``.

    Formats:
      - ``short``: today → ``HH:MM``; this year → ``DD.MM. HH:MM``; older → ``DD.MM.YYYY HH:MM``
      - ``date``:  ``Mon D, YYYY`` (e.g. ``Jun 2, 2026``)
      - ``numdate``: ``DD.MM.YYYY`` (zero-padded, no time)
      - ``long``:  ``D. M. YYYY HH:MM``
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        # Naive datetimes are assumed UTC (DB stores tz-aware, but feed parsing
        # can yield naive values); otherwise astimezone would assume system tz.
        dt = dt.replace(tzinfo=timezone.utc)
    tz = resolve_tz(tz_str)
    dt = dt.astimezone(tz)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)

    if fmt == "date":
        return f"{_MONTHS[dt.month]} {dt.day}, {dt.year}"
    if fmt == "numdate":
        pad = lambda n: str(n).zfill(2)
        return f"{pad(dt.day)}.{pad(dt.month)}.{dt.year}"
    if fmt == "long":
        return f"{dt.day}. {dt.month}. {dt.year} {dt.strftime('%H:%M')}"

    # short
    pad = lambda n: str(n).zfill(2)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return f"{pad(dt.day)}.{pad(dt.month)}. {dt.strftime('%H:%M')}"
    return f"{pad(dt.day)}.{pad(dt.month)}.{dt.year} {dt.strftime('%H:%M')}"


@cache
def _available_set() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


@cache
def available_timezone_list() -> list[str]:
    """Sorted list of IANA timezone names (cached, computed once)."""
    return sorted(_available_set())


def is_valid_timezone(tz_str: str | None) -> bool:
    return bool(tz_str) and tz_str in _available_set()


# Curated list of common timezones for the settings dropdown — covers the
# realistic user base without overwhelming with all ~600 IANA zones. Any valid
# IANA value is still accepted on save (e.g. from future browser auto-detect).
COMMON_TIMEZONES: tuple[str, ...] = (
    "UTC",
    # Europe
    "Europe/London", "Europe/Dublin", "Europe/Lisbon",
    "Europe/Paris", "Europe/Madrid", "Europe/Berlin", "Europe/Amsterdam",
    "Europe/Brussels", "Europe/Zurich", "Europe/Rome", "Europe/Vienna",
    "Europe/Prague", "Europe/Warsaw", "Europe/Budapest", "Europe/Stockholm",
    "Europe/Oslo", "Europe/Copenhagen", "Europe/Helsinki", "Europe/Athens",
    "Europe/Bucharest", "Europe/Kyiv", "Europe/Moscow", "Europe/Istanbul",
    # America
    "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "America/Toronto",
    "America/Mexico_City", "America/Bogota", "America/Lima",
    "America/Sao_Paulo", "America/Argentina/Buenos_Aires", "Pacific/Honolulu",
    # Asia
    "Asia/Jerusalem", "Asia/Dubai", "Asia/Karachi", "Asia/Kolkata",
    "Asia/Bangkok", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore",
    "Asia/Tokyo", "Asia/Seoul",
    # Africa
    "Africa/Casablanca", "Africa/Lagos", "Africa/Cairo", "Africa/Johannesburg",
    "Africa/Nairobi",
    # Oceania
    "Australia/Perth", "Australia/Sydney", "Pacific/Auckland",
)


@cache
def _common_set() -> frozenset[str]:
    return frozenset(COMMON_TIMEZONES)


def is_common_timezone(tz_str: str | None) -> bool:
    return bool(tz_str) and tz_str in _common_set()


@cache
def timezone_groups() -> list[tuple[str, list[str]]]:
    """Common timezones grouped by region (continent) for an <optgroup> select,
    preserving the curated order within each region."""
    groups: dict[str, list[str]] = {}
    for name in COMMON_TIMEZONES:
        region = name.split("/", 1)[0] if "/" in name else "UTC"
        groups.setdefault(region, []).append(name)
    # Keep region order by first appearance in the curated list.
    return list(groups.items())

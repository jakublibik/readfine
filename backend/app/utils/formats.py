"""Per-user number & date *format profile* (variant B).

A single source of truth for how numbers and dates are written, independent of
UI language and timezone. A profile bundles the decimal/thousands separators and
the date field order/separator into one named choice (``us``, ``uk``, ``eu``,
``de``, ``iso``); number and date format are intentionally coupled.

The viewer's profile is carried per-request via a ``ContextVar`` (set in the auth
dependency, mirroring ``current_viewer_tz``). Background renders (e.g. briefings)
have no request context and must pass ``profile=`` explicitly.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime

# Per-request viewer format profile. Defaults to the neutral ``iso`` so that
# unauthenticated pages, seeded rows and background renders without an explicit
# profile stay culture-neutral. Existing users are backfilled to ``eu`` by
# migration; new registrations detect from the browser.
current_viewer_format: ContextVar[str] = ContextVar("current_viewer_format", default="iso")

_DEFAULT = "iso"


@dataclass(frozen=True)
class Profile:
    key: str
    name: str            # short display name for the select label
    decimal: str         # decimal separator
    group: str           # thousands separator ("" = none)
    date_order: tuple    # e.g. ("M", "D", "Y")
    date_sep: str        # separator between date fields


# Insertion order = order shown in the settings select.
FORMAT_PROFILES: dict[str, Profile] = {
    "us": Profile("us", "US", ".", ",", ("M", "D", "Y"), "/"),
    "uk": Profile("uk", "UK/Intl", ".", ",", ("D", "M", "Y"), "/"),
    "eu": Profile("eu", "Europe", ",", " ", ("D", "M", "Y"), "."),
    "de": Profile("de", "DE/AT", ",", ".", ("D", "M", "Y"), "."),
    "iso": Profile("iso", "ISO", ".", "", ("Y", "M", "D"), "-"),
}


def resolve_profile(key: str | None) -> Profile:
    return FORMAT_PROFILES.get(key or "", FORMAT_PROFILES[_DEFAULT])


def is_valid_format(key: str | None) -> bool:
    return bool(key) and key in FORMAT_PROFILES


def format_number(value, decimals: int | None = None, profile: str | None = None) -> str:
    """Format a number per the given (or current viewer's) profile.

    ``decimals=None`` → integer with grouping; otherwise fixed decimals. Uses
    Python ``format`` (round-half-even), which also unifies rounding across the
    templates. ``None``/non-numeric values render as an empty string. (Jinja
    ``Undefined`` is coerced to ``None`` by the ``num`` filter before it gets here.)
    """
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    p = resolve_profile(profile if profile is not None else current_viewer_format.get())
    s = f"{num:,.0f}" if decimals is None else f"{num:,.{decimals}f}"
    # Python emits "," group + "." decimal; swap to the profile via a placeholder
    # so the two replacements can't clash.
    return s.replace(",", "\x00").replace(".", p.decimal).replace("\x00", p.group)


def format_number_g(value, profile: str | None = None) -> str:
    """Minimal-digits number (like printf ``%g``: no trailing zeros) with the
    profile's decimal separator. For small values such as fetch intervals in hours
    where ``num``'s fixed decimals would show ``6,0h`` instead of ``6h``."""
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    p = resolve_profile(profile if profile is not None else current_viewer_format.get())
    return f"{num:g}".replace(".", p.decimal)


def format_thousands(value) -> str:
    """Integer grouped with a space, regardless of the viewer's profile.

    For the numeric settings inputs and the sentences that quote their bounds.
    Deliberately not ``format_number``: those values sit in text fields that
    ai-settings.js regroups with spaces as you type, so a profile grouping with
    "," or "." would make the field jump on the first keystroke.
    """
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return ""


def format_date_parts(dt: date | datetime, profile: Profile, with_year: bool = True) -> str:
    """Assemble a zero-padded numeric date in the profile's field order/separator.

    ``with_year=False`` drops the year (used by the relative "this year" short form).
    """
    parts: list[str] = []
    for token in profile.date_order:
        if token == "Y":
            if with_year:
                parts.append(str(dt.year))
        elif token == "M":
            parts.append(f"{dt.month:02d}")
        elif token == "D":
            parts.append(f"{dt.day:02d}")
    return profile.date_sep.join(parts)


# Sample used to render the human-readable example in each select label.
_SAMPLE_DATE = date(2026, 6, 25)  # day > 12 so it is unambiguously the day


def format_choices() -> list[tuple[str, str]]:
    """(key, label) pairs for the settings select, e.g.
    ``("eu", "1 234,56 · 25.06.2026 (Europe)")``. Example generated from the profile."""
    out = []
    for p in FORMAT_PROFILES.values():
        num = format_number(1234.56, 2, p.key)
        dat = format_date_parts(_SAMPLE_DATE, p)
        out.append((p.key, f"{num} · {dat} ({p.name})"))
    return out

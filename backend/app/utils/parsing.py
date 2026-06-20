import re
from urllib.parse import urljoin


def rewrite_relative_urls(html: str, base_url: str) -> str:
    """Rewrite relative src/href attributes in sanitized HTML to absolute URLs."""
    def _abs(m: re.Match) -> str:
        attr, url = m.group(1), m.group(2)
        return f'{attr}="{urljoin(base_url, url)}"'
    return re.sub(r'(src|href)="([^"]*)"', _abs, html)


def safe_int(value, default=None) -> int | None:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def clamp(value: int | None, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    return max(lo, min(hi, value))

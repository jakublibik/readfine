"""Plain-text helpers shared across services (snippets, previews, profile text)."""
import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_html(text: str | None) -> str:
    """Strip HTML tags and collapse whitespace to single spaces.

    A cheap regex strip for building display snippets / previews / AI-profile text
    from stored article bodies. **Not** sanitization: it drops tags for readability,
    it does not neutralize malicious markup. Use ``nh3.clean`` on any path where the
    result is rendered as HTML.
    """
    if not text:
        return ""
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()

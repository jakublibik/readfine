"""Plain-text helpers shared across services (snippets, previews, profile text)."""
import html
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


# The types feedparser gives a title it hands over as markup: Atom type="html" or
# "xhtml", or an RSS title it found entities or tags in (for RSS it guesses). A
# title of any other type comes back already decoded.
_MARKUP_TYPES = frozenset({"text/html", "application/xhtml+xml"})


def feed_title_text(value: str | None, detail) -> str | None:
    """A feed or entry title as the plain text it is stored and shown as.

    feedparser returns a title of Atom type="html" as markup, entities included, so
    WordPress's `&#8216;eSUV&#8217;` would be stored as those characters and shown
    literally. Tags are dropped without a space, since in a title they mark up a word
    (`<em>Dune</em>'s`) rather than separate blocks. `detail` is the entry's or the
    feed's `title_detail`.
    """
    if not value or not detail or detail.get("type") not in _MARKUP_TYPES:
        return value
    return _WHITESPACE_RE.sub(" ", html.unescape(_HTML_TAG_RE.sub("", value))).strip()

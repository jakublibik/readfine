import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import nh3


# Tracking parameters that identify the referrer rather than the article, so two
# links to the same piece must compare equal without them.
_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "fbclid", "gclid", "msclkid",
})


def normalize_url(url: str | None) -> str | None:
    """The comparable form of an article address, used as a dedup key.

    Lowercases scheme and host, drops the fragment, trailing slash and tracking
    parameters, and sorts what remains. Returns None for anything that is not an
    http(s) URL.

    Credentials in the netloc are dropped as well. They say who is asking, not which
    article this is, and a key holding a password would put it in a second column
    after the work done to keep it out of ``feeds.feed_url``.
    """
    if not url:
        return None
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            return None
        path = p.path.rstrip("/") or "/"
        netloc = p.netloc.rsplit("@", 1)[-1].lower()
        params = [(k, v) for k, v in sorted(parse_qsl(p.query)) if k not in _STRIP_PARAMS]
        return urlunparse((p.scheme.lower(), netloc, path, "", urlencode(params), ""))[:2048]
    except Exception:
        return None


def rewrite_relative_urls(html: str, base_url: str) -> str:
    """Rewrite relative src/href attributes in sanitized HTML to absolute URLs."""
    def _abs(m: re.Match) -> str:
        attr, url = m.group(1), m.group(2)
        return f'{attr}="{urljoin(base_url, url)}"'
    return re.sub(r'(src|href)="([^"]*)"', _abs, html)


# ── non-breaking space runs ───────────────────────────────────────────────────
# CMS editors (WordPress, Word paste) sprinkle &nbsp; around inline formatting, so a
# whole phrase like "Wednesday,<nbsp><strong>Anthropic</strong><nbsp>and<nbsp>..."
# becomes one unbreakable word that overflows a narrow reading panel. We break the
# longest runs apart by turning selected nbsp back into ordinary spaces, keeping the
# ones that look deliberate (units, numbers, one-letter prepositions).

NBSP_RUN_LIMIT = 25

# Everything else (including <br> and <wbr>) ends a run.
_INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em",
    "i", "ins", "kbd", "mark", "q", "rp", "rt", "ruby", "s", "samp", "small",
    "span", "strong", "sub", "sup", "time", "u", "var",
}
_NBSP_ATOMS = {
    " ", " ",
    "&nbsp;", "&#160;", "&#xa0;", "&#x00a0;", "&#8239;", "&#x202f;",
}
_TAG_NAME_RE = re.compile(r"</?\s*([a-zA-Z0-9-]+)")
_TOKEN_RE = re.compile(r"(<[^>]*>)")
# One atom = one rendered character: an entity or a single character.
_ATOM_RE = re.compile(r"&#?\w+;|.", re.S)
_NUMERIC_RE = re.compile(r"^[0-9]+([.,][0-9]+)*[.,]?$")
_MARKER_RE = re.compile(r"^([^\W\d_]{1,3}\.|[§#%‰°$€£])$")


def _is_deliberate(left: str, right: str) -> bool:
    """True if the nbsp between these two words is likely intentional typography."""
    if _NUMERIC_RE.match(left):          # 10 km, 50 %, 2 000
        return True
    if len(left) == 1 and left.isalpha():  # Czech one-letter prepositions: v, a, k, s, o
        return True
    if _MARKER_RE.match(left) and _NUMERIC_RE.match(right):  # str. 5, § 12
        return True
    return False


def _pick_breaks(seg_texts: list[str], limit: int) -> list[int]:
    """Indices of the separators to turn into ordinary spaces.

    Splits the run near its middle and recurses into both halves, so even a very long
    run ends up as pieces that fit on a line.
    """
    seg_lens = [len(s) for s in seg_texts]
    chosen: list[int] = []

    def walk(lo: int, hi: int) -> None:
        total = sum(seg_lens[lo:hi + 1]) + (hi - lo)
        if total <= limit or lo == hi:
            return
        best, best_diff = None, None
        left = seg_lens[lo]
        for i in range(lo, hi):
            if not _is_deliberate(seg_texts[i], seg_texts[i + 1]):
                diff = abs(left - (total - left - 1))
                if best_diff is None or diff < best_diff:
                    best, best_diff = i, diff
            left += seg_lens[i + 1] + 1
        if best is None:  # every separator looks deliberate — leave it to the CSS fallback
            return
        chosen.append(best)
        walk(lo, best)
        walk(best + 1, hi)

    walk(0, len(seg_texts) - 1)
    return chosen


def soften_nbsp_runs(html: str, limit: int = NBSP_RUN_LIMIT) -> str:
    """Replace non-breaking spaces that make a text run longer than `limit` characters.

    Operates on sanitized HTML: tags and attributes are left untouched, only nbsp
    inside text is converted (runs are tracked across inline tags, since the nbsp
    typically sits right next to a <strong>).
    """
    if not html or not any(atom in html for atom in (" ", " ", "&nbsp;", "&#")):
        return html

    tokens = _TOKEN_RE.split(html)
    replacements: dict[int, list[tuple[int, int]]] = {}
    seg_texts: list[str] = [""]
    seps: list[tuple[int, int, int]] = []  # (token index, start, end) of each nbsp atom

    def flush() -> None:
        nonlocal seg_texts, seps
        if seps:
            for i in _pick_breaks(seg_texts, limit):
                token_idx, start, end = seps[i]
                replacements.setdefault(token_idx, []).append((start, end))
        seg_texts, seps = [""], []

    for idx, token in enumerate(tokens):
        if token.startswith("<"):
            name = _TAG_NAME_RE.match(token)
            if not name or name.group(1).lower() not in _INLINE_TAGS:
                flush()
            continue
        for atom in _ATOM_RE.finditer(token):
            text = atom.group(0)
            if text in _NBSP_ATOMS:
                seps.append((idx, atom.start(), atom.end()))
                seg_texts.append("")
            elif text.isspace():
                flush()
            else:
                seg_texts[-1] += text
    flush()

    if not replacements:
        return html
    for idx, spans in replacements.items():
        token = tokens[idx]
        for start, end in sorted(spans, reverse=True):
            token = token[:start] + " " + token[end:]
        tokens[idx] = token
    return "".join(tokens)


def count_words(html: str | None) -> int:
    """Words in an HTML body, tags stripped. Shared by every place that measures how
    much text a body holds, so reading time, the full-content detector and the
    subscribe heuristic all count the same way."""
    if not html:
        return 0
    plain = nh3.clean(html, tags=set())
    return len(re.findall(r"\w+", plain))


def safe_int(value, default=None) -> int | None:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


def clamp(value: int | None, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    return max(lo, min(hi, value))

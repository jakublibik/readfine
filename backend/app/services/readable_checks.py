"""What a fetched page says about itself, and whether an extraction matches it.

Metadata read from the page's head (title, og:description, canonical address), and
the checks built on it: did the extraction land on the article or on a substitute
page (a consent wall, a login prompt, a different article), and is the address the
page claims for itself one we should adopt.
"""
import html as html_mod
import re
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

import nh3

from app.utils.parsing import count_text_words


_OG_TITLE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["']og:title["'][^>]*\bcontent\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

_HEAD_END_RE = re.compile(r"</head\s*>", re.IGNORECASE)
# How far in to look for the closing tag. A fixed byte prefix used to stand in for the
# head and silently lost the metadata of any page that opens with a large inline script
# block: a YouTube watch page carries its <title>, og: tags and rel=canonical past
# 680 KB, so a 200 KB window saw none of them — no title, and, worse, no description for
# _content_contradicts_page to judge the extraction against.
_HEAD_SCAN_BYTES = 1_000_000
# Used only when no </head> turns up inside that cap, i.e. the markup is broken or the
# response is not HTML at all. Scanning the body of a long article for a title is waste.
_HEAD_FALLBACK_BYTES = 200_000


def _head_slice(html: str) -> str:
    """The document's ``<head>``, which is where every metadata regex below looks.

    Bounded by the closing tag rather than by a byte count, because how far in the
    metadata sits is a property of the page, not something a constant can predict.
    The regexes themselves stay cheap even on a pathological head: ~1.5 ms for all
    four over YouTube's 694 KB.
    """
    m = _HEAD_END_RE.search(html, 0, _HEAD_SCAN_BYTES)
    return html[: m.end()] if m else html[:_HEAD_FALLBACK_BYTES]


_OG_DESC_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["']og:description["'][^>]*\bcontent\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_OG_DESC_ALT_RE = re.compile(
    r"""<meta[^>]+\bcontent\s*=\s*["']([^"']+)["'][^>]*(?:property|name)\s*=\s*["']og:description["']""",
    re.IGNORECASE,
)
# Below this many distinct words the overlap score is too noisy to act on, so the
# check is skipped entirely rather than guessed at.
_OG_DESC_MIN_WORDS = 10
# Measured over ~40 live articles (news, blogs, docs, science) in both English and
# Czech: legitimate extractions scored 0.24–1.00 and substitute pages 0.00–0.03, so a
# threshold here sits between the two. Most legitimate pages are far above it, because
# the lede is usually the article's own first paragraph and nearly every word of it
# reappears; what pulls the low end down is a paywall teaser, where only the opening
# survives and the description reaches past it (Washington Post 0.30, Novinky 0.24).
# The gap is real but narrower than it looks, so treat a raise as needing fresh
# measurement rather than reasoning.
_OG_DESC_MIN_OVERLAP = 0.15

_WRONG_CONTENT_MSG = (
    "The site returned a consent or paywall page instead of the article"
)

_BOT_WALL_MSG = (
    "The site asked for a browser check instead of showing the article"
)

# Phrases a browser check says and an article does not. Matched against the extracted
# body, lowercased, with runs of whitespace collapsed, so a phrase broken across lines
# in the source still matches.
_BOT_WALL_PHRASES = (
    "enable cookies",
    "cookies must be enabled",
    "cookies are disabled",
    "enable javascript",
    "javascript is disabled",
    "javascript is required",
    "requires javascript",
    "turn on javascript",
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "are you a robot",
)

# A wall is a handful of words; an article that happens to mention one of the phrases
# is not. PubMed's is 10 words and a Cloudflare interstitial around 30, while the
# shortest legitimate extraction in the survey corpus is xkcd at 58 and the shortest
# with any prose in it is Apple at 78. The cap sits well above the walls and below
# anything that reads as writing, and it has to be cleared *as well as* a phrase.
_BOT_WALL_MAX_WORDS = 120


_CANONICAL_RE = re.compile(
    r"""<link[^>]+rel\s*=\s*["']canonical["'][^>]*\bhref\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_OG_URL_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["']og:url["'][^>]*\bcontent\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


def _same_host(a: Optional[str], b: Optional[str]) -> bool:
    """Host equality, ignoring a leading ``www.``.

    Deliberately not registrable-domain equality: the redirect this guards against —
    news.google.com to consent.google.com — shares its registrable domain with the
    address it replaced, so folding subdomains together would wave it straight
    through.
    """
    try:
        return (urlsplit(a or "").netloc.lower().removeprefix("www.")
                == urlsplit(b or "").netloc.lower().removeprefix("www."))
    except ValueError:
        return False


def resolve_article_url(
    fetched_url: Optional[str], html: Optional[str], requested_url: Optional[str] = None
) -> Optional[str]:
    """The address an article actually lives at, given where the fetch ended up.

    Prefers the page's own ``rel=canonical`` / ``og:url`` over the fetched address,
    which strips campaign parameters, session ids and AMP variants — but **only when
    it is on the same host**. A syndicated article routinely names the original
    publisher's domain as its canonical, and following that across hosts would let
    one article's URL resolve onto a different article's row, which for dedup is far
    worse than the cosmetic problem this fixes.

    *requested_url* is the address that was asked for, before redirects. When the
    chain ends on a **different host** and the page there does not name an address of
    its own, the fetch did not arrive at an article: it arrived at an interstitial.
    Pasting a Google News link lands on consent.google.com, which carries neither
    canonical nor og:url, and adopting that address makes it the saved article's
    permanent home — "Open original" and Retry then both walk back into the consent
    page. Keeping the requested address instead costs at worst an unstripped tracker,
    which is where the article started anyway.

    A legitimate cross-host redirect is unaffected, because the page it lands on says
    who it is: doi.org to nature.com, youtu.be to a watch URL and m.wikipedia to the
    desktop host all carry both tags. Omit *requested_url* (the default) to skip the
    check entirely — the redirect chain is then unknown, and no verdict is possible.
    """
    if not fetched_url:
        return None
    declared = _declared_url(fetched_url, html)
    if declared:
        return declared
    if requested_url and not _same_host(fetched_url, requested_url):
        return requested_url
    return fetched_url


def redirected_back_to_us(
    fetched_url: Optional[str], requested_url: Optional[str], html: Optional[str]
) -> bool:
    """True when the fetch landed on a page whose job is to send us back.

    A consent or login wall carries the address it interrupted, so it can return the
    visitor there once they submit: iDNES answers a server-side fetch with
    ``/nastaveni-souhlasu?url=<the article>``, Google News with a consent page holding
    ``continue=<the article>``. That round trip is the interstitial's own signature and
    it needs no wordlist, no language and no per-site rule to read.

    It also catches what ``resolve_article_url`` cannot: that check only doubts a
    redirect leaving the host, and iDNES never leaves idnes.cz, so the consent page was
    adopted as the article's own address and "Open original" led back into it.

    Three things are required, each of them there to keep a real article out of this:

    * the chain moved somewhere else, so nothing that served the requested address is
      ever judged;
    * a query value holds the whole requested address or its whole path, not merely a
      substring of one, so a stray ``?ref=/`` cannot trip it;
    * the page does not claim an address of its own. A document viewer legitimately
      built around ``?url=`` says so with rel=canonical or og:url, and is waved through,
      the same escape hatch cross-host redirects already get. A canonical pointing at
      the carried article is no such claim: the iDNES wall ships exactly that.
    """
    if not fetched_url or not requested_url or fetched_url == requested_url:
        return False
    query = urlsplit(fetched_url).query
    if not query:
        return False
    wanted = {requested_url, urlsplit(requested_url).path}
    wanted.discard("")
    wanted.discard("/")
    carried = any(
        value in wanted or urlsplit(value).path in wanted
        for _, value in parse_qsl(query, keep_blank_values=False)
    )
    if not carried:
        return False
    # A page naming an address of its own is a viewer built around ?url=, not a wall.
    # Naming the carried article does not count: iDNES's consent page copies the
    # interrupted article's canonical into its head, so that claim is the wall's too.
    declared = _declared_url(fetched_url, html)
    if not declared or declared == fetched_url:
        return True
    return declared in wanted or urlsplit(declared).path in wanted


def _declared_url(fetched_url: str, html: Optional[str]) -> Optional[str]:
    """The absolute, same-host address the page claims for itself, if it claims one."""
    if not html:
        return None
    head = _head_slice(html)
    m = _CANONICAL_RE.search(head) or _OG_URL_RE.search(head)
    if not m:
        return None
    candidate = m.group(1).strip()
    if not candidate.startswith(("http://", "https://")):
        return None
    try:
        if urlsplit(candidate).netloc.lower() != urlsplit(fetched_url).netloc.lower():
            return None
    except ValueError:
        return None
    return candidate


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"\w{4,}", text.lower())}


# A well-formed entity left over *after* decoding, which means the source escaped its
# text twice. Named entities are matched by shape rather than by name because the point
# is only to recognise that another pass is warranted, not to decode anything here.
_LEFTOVER_ENTITY_RE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")


def _unescape_text(raw: str) -> str:
    """Decode HTML entities in page metadata, double-encoding included.

    Some sites escape their text twice, so what reaches us is ``&amp;#x27;`` and one
    decode leaves a visible ``&#x27;`` in the title and the description (Vimeo does this
    across every page). A second pass is taken only when the first one left a
    well-formed entity behind, so ordinary text is decoded exactly once.

    Stops at two passes rather than looping to a fixed point: triple-encoding is not a
    thing worth chasing, and text that genuinely *writes about* entities should not be
    unwound arbitrarily far. Callers of this function pass plain text that is escaped
    again before it is rendered, so an extra pass cannot revive markup.
    """
    once = html_mod.unescape(raw)
    return html_mod.unescape(once) if _LEFTOVER_ENTITY_RE.search(once) else once


def _extract_og_description(html: str) -> Optional[str]:
    head = _head_slice(html)
    m = _OG_DESC_RE.search(head) or _OG_DESC_ALT_RE.search(head)
    if not m:
        return None
    return re.sub(r"\s+", " ", _unescape_text(m.group(1))).strip()


def _content_contradicts_page(content_html: str, og_description: Optional[str]) -> bool:
    """True when the extracted text plainly is not the article the page describes.

    Some sites answer a server-side fetch with HTTP 200 and a consent/paywall page
    instead of the article. Nothing downstream can tell: the status is fine, the
    length is respectable, and the extractor faithfully returns the only prose on
    the page — which is the cookie notice. Stored as-is, that reads as an article
    made of advertising copy.

    The publisher's own og:description is the check: on a real article it is the
    lede, so almost all of it reappears in the body. On a substitute page it shares
    nothing. Returns False whenever there is not enough to judge on.

    **Known blind spot**: a page with no usable description is never judged, and that
    is roughly a quarter of the live sample this was measured on (Wikipedia, Nature,
    Hacker News and Substack all ship without one). A substitute page served there
    passes — a Cloudflare "Client Challenge" and a Google consent page both do. Two
    replacements were measured and rejected rather than left unbuilt. Scoring the body
    against the page's ``<title>`` inverts on exactly these pages: the title describes
    the interstitial and the body *is* the interstitial, so consent pages scored 1.00,
    the top of the legitimate range. Scoring it against the words in the pasted URL's
    slug does separate them in English (0.00 against a 0.33 floor) but collapses in
    Czech, where inflection alone dropped a genuine article to 0.18, below the 0.20 of
    a page that was in fact substituted, and roughly a third of English articles carry
    an opaque id instead of a slug. Neither is worth the false rejections.
    """
    if not og_description:
        return False
    desc_words = _words(og_description)
    if len(desc_words) < _OG_DESC_MIN_WORDS:
        return False
    body_words = _words(nh3.clean(content_html, tags=set()))
    if not body_words:
        return False
    overlap = len(desc_words & body_words) / len(desc_words)
    return overlap < _OG_DESC_MIN_OVERLAP


def _looks_like_a_bot_wall(content_html: str) -> bool:
    """True when the extracted body is a browser check rather than an article.

    The sibling of ``_content_contradicts_page`` for the blind spot that one names:
    a substitute page with no og:description to be judged against. PubMed is the case
    that prompted it. It answers a server-side fetch with HTTP 203 and 5.5 kB of
    JavaScript proof-of-work, and the only prose on it is "Enable cookies for
    pubmed.ncbi.nlm.nih.gov and reload this page to continue." Stored as-is, the reader
    gets a ten-word article that looks like the article. Cloudflare's interstitial and
    Anubis land the same way.

    The abstract itself is out of reach and stays that way: the cookie is computed by
    the challenge script, so there is nothing to send on a second request (measured
    with a cookie jar, which changes nothing), and running the script would mean a
    headless browser. What this does is make the failure honest, so the reader gets
    the error and the "Open original" button instead of the wall as an article.

    Deliberately a phrase match and not a similarity score. The two scores that could
    have covered the same blind spot were measured and rejected, for reasons written
    out in ``_content_contradicts_page``; a wall, unlike a paywall teaser, says a
    specific small set of things that articles do not say. Both halves are required,
    so a piece *about* cookie banners is safe as long as it is longer than a wall,
    and one shorter than 120 words that also tells you to enable cookies is a wall.
    """
    text = " ".join(nh3.clean(content_html, tags=set()).lower().split())
    if not text or count_text_words(text) > _BOT_WALL_MAX_WORDS:
        return False
    return any(phrase in text for phrase in _BOT_WALL_PHRASES)


def _extract_title(html: str) -> Optional[str]:
    """Page title from og:title, falling back to <title>.

    Deliberately regex over the head rather than a parse: the only caller is the
    save-by-URL path, and building a readability Document (or a second BeautifulSoup
    tree) just for a title would put a full lxml parse on every extraction if this
    ever moved onto the shared path.
    """
    head = _head_slice(html)
    for pattern in (_OG_TITLE_RE, _TITLE_RE):
        m = pattern.search(head)
        if not m:
            continue
        title = _unescape_text(m.group(1))
        title = re.sub(r"\s+", " ", title).strip()
        if title:
            return title[:1000]
    return None

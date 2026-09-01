"""Readable extraction pipeline: trafilatura → readability-lxml fallback."""
import html as html_mod
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
import nh3
import trafilatura
from bs4 import BeautifulSoup
from readability import Document
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article
from app.models.feed import Feed, UserFeed
from app.services.ai_jobs import BACKOFF_MINUTES, MAX_RETRIES
from app.utils.crypto import auth_pair, feed_auth
from app.utils.http_client import READFINE_UA
from app.utils.parsing import count_words, rewrite_relative_urls, soften_nbsp_runs
from app.utils.video import collect_video_figures, video_page_content, video_target

logger = logging.getLogger(__name__)

# Extraction settings. MAX_RETRIES / BACKOFF_MINUTES are shared with the AI job
# services (app.services.ai_jobs) so the retry cadence stays consistent; readable's
# own failure handling (below) applies them to Article rows, not ArticleAiJob.
_TIMEOUT = 15  # seconds per HTTP request
_BATCH_SIZE = 20  # articles processed per scheduler run
_MAX_REDIRECTS = 5  # maximum followed redirects per request

# Auto-disable threshold: if this fraction of sampled articles carry more than
# _FULL_CONTENT_MIN_WORDS words in the feed itself, the feed is considered full-content
# and readable extraction is disabled.
_FULL_CONTENT_THRESHOLD = 0.8
_FULL_CONTENT_SAMPLE = 10  # how many recent articles to sample
_FULL_CONTENT_MIN_WORDS = 500
# How much of each body is read to answer "more than _FULL_CONTENT_MIN_WORDS words?".
# 500 words of prose is some 3 kB, so 20 kB leaves room for markup several times the
# text it wraps; a body that needs more than this to reach 500 words does not exist
# outside a generated page.
_FULL_CONTENT_SAMPLE_CHARS = 20_000

# Error message used when extraction yields no usable content (page produced nothing,
# or the result collapsed to whitespace after sanitization, e.g. Reddit comment pages).
_EMPTY_CONTENT_MSG = "No content could be extracted from the page"

# The page went past the download cap. Said without the cap's own size, which is a
# server setting the reader cannot act on; the byte count goes to the log instead.
_TOO_LARGE_MSG = "The page is too large to download"


# ── core extraction ───────────────────────────────────────────────────────────

def _fetch_html(
    url: str, auth_user: Optional[str], auth_pass: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    """Download article HTML.

    Returns (html, error_message, http_status_code, final_url). *final_url* is where
    the redirect chain actually ended — the caller pasted address may be a click
    tracker or carry campaign parameters, and for a saved article that address is
    what ends up on screen and in the dedup key.

    The download itself is handed to :func:`fetch_url_page`, the same SSRF-safe path
    the feed fetcher uses, rather than being repeated here. Validating an address and
    then letting the client resolve it again leaves a window in which DNS can answer
    differently the second time (rebinding to 169.254.169.254 and friends); the shared
    path closes it by connecting to the IP it validated, on every hop. That window
    used to be reachable only while subscribing to a feed, but save-by-URL fetches
    any address on request and repeatedly, which is what a race needs.
    """
    from app.utils.url_validator import ResponseTooLarge, fetch_url_page
    auth = auth_pair(auth_user, auth_pass)
    try:
        page = fetch_url_page(
            url,
            auth=auth,
            timeout=_TIMEOUT,
            headers={"User-Agent": READFINE_UA},
            max_redirects=_MAX_REDIRECTS,
        )
        return page.text, None, None, page.final_url
    except ValueError as exc:
        # A blocked address, ours or one a Location header pointed at (the message
        # says which). Not an HTTP failure, so there is no status to report.
        logger.warning("readable URL blocked (SSRF): %s — %s", url, exc)
        return None, str(exc), None, None
    except ResponseTooLarge as exc:
        # Not an HTTP failure and not a bad address: the host answered, the answer
        # was just bigger than we are willing to hold.
        logger.warning("readable fetch abandoned for %s: %s", url, exc)
        return None, _TOO_LARGE_MSG, None, None
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        msg = f"HTTP {status_code} {exc.response.reason_phrase}"
        logger.warning("readable fetch failed for %s: %s", url, msg)
        return None, msg, status_code, None
    except httpx.TimeoutException:
        msg = f"Timeout after {_TIMEOUT}s"
        logger.warning("readable fetch timed out for %s", url)
        return None, msg, None, None
    except Exception as exc:
        msg = str(exc)[:200]
        logger.warning("readable fetch failed for %s: %s", url, msg)
        return None, msg, None, None


def _strip_pre_extraction_noise(html: str) -> str:
    """Remove page chrome that extractors mistake for the main content.

    Tumblr renders a large `<ol class="notes">` list of likes/reblogs (inside
    `#notecontainer`). It is the biggest block on a short post, so trafilatura's
    precision heuristics latch onto it and return the notes list *instead* of the
    post body — and because that result is non-empty, the readability fallback never
    runs. Dropping it before extraction lets the real post surface. We also drop
    `<noscript>` here: Tumblr posts carry a tracking-pixel `<noscript>` that
    readability keeps as escaped `<img>` text, which renders as garbage in the body.
    """
    if "notecontainer" not in html and 'class="notes"' not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select("#notecontainer, ol.notes"):
        el.decompose()
    for el in soup.find_all("noscript"):
        el.decompose()
    return str(soup)


_MEDIAWIKI_RE = re.compile(
    r"""<meta[^>]+name\s*=\s*["']generator["'][^>]*\bcontent\s*=\s*["']MediaWiki""",
    re.IGNORECASE,
)

# Inside a lifted box or table: navigation, print-only variants and rows the page
# itself hides. The hidden ones matter because our sanitizer strips `style`, so
# anything MediaWiki had set to display:none (a second copy of the coordinates,
# "Show map of Europe") would come back visible.
_MEDIAWIKI_NOISE = (
    '.mw-editsection, .noprint, .nomobile, .mw-empty-elt, style, [style*="display:none"]'
)

# Where a lifted table is put back. It has to be something trafilatura keeps verbatim
# and in place: a bare <p> of text does both (checked over 14 tables on 5 articles),
# while a <div> is dropped outright.
_TABLE_MARKER = "RFDATATABLE"
_TABLE_MARKER_P_RE = re.compile(rf"<p>\s*{_TABLE_MARKER}(\d+)\s*</p>")
_TABLE_MARKER_BARE_RE = re.compile(rf"{_TABLE_MARKER}(\d+)(?!\d)")
# Where the box names itself. Templates differ on which of these they use, and a good
# third of them use none, hence the fall back to the leading header cell.
_INFOBOX_TITLE_SELECTOR = "caption, .infobox-above, .infobox-title"

# Above this, an infobox is a screenful of table standing between the reader and the
# article's first sentence, which on a phone is worse than not having it, so it starts
# collapsed. Measured across a spread of articles: Coffee 44 words, Ucchusma 141, Brno
# 211, Karel Čapek 227, Python 270, Prague 366, Waterloo 383. The line falls between the
# boxes that read as a caption and the ones that read as a second article. Rows are
# counted too because a box can be long without being wordy (Brno: 211 words, 39 rows).
_INFOBOX_OPEN_MAX_WORDS = 150
_INFOBOX_OPEN_MAX_ROWS = 20

_INFOBOX_FALLBACK_TITLE = "Infobox"


def _infobox_title(box) -> str:
    """What to put on the box's summary line, taken off the box itself.

    Removes the element it took the title from, so an expanded box does not show its
    own name twice. Falls back to a plain label rather than to the article title: this
    runs on stored content, which no template gets to re-render per reader, so there is
    nothing here to translate and nothing that changes if the article is renamed.
    """
    el = box.select_one(_INFOBOX_TITLE_SELECTOR)
    if el is None:
        first = box.find(["th", "td"])
        # Only a full-width header cell names the box. A plain first cell is the label
        # of a data row ("Latte and black filtered coffee"), not a title.
        if first is not None and first.name == "th" and first.get("colspan"):
            el = first
    if el is None:
        return _INFOBOX_FALLBACK_TITLE
    title = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
    # Take the whole row when the title was all of it, or the table keeps an empty
    # <tr> where the header used to be and renders a blank band above the first field.
    row = el.find_parent("tr")
    el.decompose()
    if row is not None and not row.find(["th", "td"]):
        row.decompose()
    return title[:200] or _INFOBOX_FALLBACK_TITLE


class MediaWikiChrome(NamedTuple):
    """A MediaWiki page taken apart: what to extract, and what to put back after."""
    html: str
    infoboxes: list[str] = []
    tables: list[str] = []


def _lift_mediawiki_chrome(html: str) -> MediaWikiChrome:
    """Take a MediaWiki page apart before extraction, keeping what is worth keeping.

    trafilatura is not to be trusted with a table on a page this size. On the Prague
    article it closes the climate table right after the first header cell and drops
    every remaining value into the body as its own paragraph, which is how a weather
    table came to read as a column of bare numbers. It is not the table's complexity:
    handed that same table on its own, trafilatura returns all 12 rows and 142 cells.
    So each one is lifted out here and put back afterwards, and only the page's prose
    is handed to the extractor.

    Four things happen, and everything but the navboxes is kept:

    **``[edit]`` links** are dropped. Every section heading on a MediaWiki page carries
    one, and trafilatura's precision pass prunes a heading that contains a link, so the
    whole article arrived as one unbroken wall of text. This is the larger half of the
    fix and it costs nothing: on the article this was reported for, dropping them takes
    it from zero headings to eleven, and nobody wants ``[edit]`` in a reader.

    **The infobox** is lifted whole and comes back wrapped in ``<details>``, so a long
    one is a single line on a phone rather than three screens of table before the lede.
    The caller puts it at the top, where the page had it.

    **Data tables** (``.wikitable``) are lifted the same way, but they belong where
    they stood, so each leaves a marker paragraph behind for the caller to swap back.

    **Navboxes** are dropped outright. They are the navigation footers ("Districts of
    Prague", "Capitals of Europe"), they are worth nothing to a reader, and trafilatura
    was leaking pieces of them into the article as mangled table fragments.
    """
    if not _MEDIAWIKI_RE.search(_head_slice(html)):
        return MediaWikiChrome(html)
    soup = BeautifulSoup(html, "html.parser")
    for el in soup.select(".mw-editsection, .navbox, .navbox-inner, .navbox-subgroup"):
        el.decompose()

    boxes: list[str] = []
    for box in soup.select("table.infobox"):
        box.extract()
        for junk in box.select(_MEDIAWIKI_NOISE):
            junk.decompose()
        title = _infobox_title(box)
        rows = len(box.find_all("tr"))
        words = len(box.get_text(" ", strip=True).split())
        if not rows and not words:
            continue  # the box was chrome all the way down
        opened = " open" if words <= _INFOBOX_OPEN_MAX_WORDS and rows <= _INFOBOX_OPEN_MAX_ROWS else ""
        # The title is page text being put back into markup, so it is escaped here
        # rather than left for the sanitizer: nh3 would strip a tag it found, but the
        # text around it would still have been reparsed as markup first.
        boxes.append(
            f"<details data-infobox{opened}>"
            f"<summary>{html_mod.escape(title)}</summary>{box}</details>"
        )

    # After the infoboxes, so a table nested in one is not lifted out from under it.
    tables: list[str] = []
    for table in soup.select("table.wikitable"):
        marker = soup.new_tag("p")
        marker.string = f"{_TABLE_MARKER}{len(tables)}"
        table.replace_with(marker)
        for junk in table.select(_MEDIAWIKI_NOISE):
            junk.decompose()
        tables.append(str(table))

    return MediaWikiChrome(str(soup), boxes, tables)


def _restore_wiki_tables(body: str, tables: list[str]) -> str:
    """Put the lifted data tables back where their markers ended up.

    A marker that did not survive extraction means the section holding it was pruned
    away. The table is appended rather than dropped, because losing a page's data
    outright is the one outcome worse than showing it out of order.
    """
    clean = [_sanitize(table) for table in tables]
    placed: set[int] = set()

    def _swap(match: re.Match) -> str:
        index = int(match.group(1))
        if index in placed or index >= len(clean):
            return ""  # a repeated or unknown marker is text nobody should read
        placed.add(index)
        return clean[index]

    # The marker comes back as a paragraph of its own, so that is the shape to look
    # for first; the bare form is only there in case something wrapped it in prose.
    body = _TABLE_MARKER_P_RE.sub(_swap, body)
    body = _TABLE_MARKER_BARE_RE.sub(_swap, body)
    missing = [table for i, table in enumerate(clean) if i not in placed]
    return body + "".join(missing)


def _to_html_tags(fragment: str) -> str:
    """Rewrite trafilatura's own XML vocabulary into the HTML the sanitizer keeps.

    ``output_format="html"`` is only mostly HTML. An image comes back as
    ``<graphic/>`` and a table as ``<table><row><cell>``, and of those only
    ``<table>`` is in the allowlist, so ``row`` and ``cell`` were dropped by the
    sanitizer while their text was kept — every table collapsed into a run of loose
    paragraphs in the middle of the article. A Wikipedia infobox is the loudest case
    but any page carrying a table hits it.

    ``<graphic>`` is rewritten by regex, before the parse rather than during it:
    html.parser has no idea the element is void, so ``<graphic/>`` would open a tag
    that never closes and swallow the rest of the document into it. ``row`` and
    ``cell`` arrive properly paired, so they are safe to rename on the tree.
    """
    fragment = re.sub(r'<graphic\b([^>]*)/>', r'<img\1>', fragment)
    if "<row" not in fragment and "<cell" not in fragment:
        return fragment
    soup = BeautifulSoup(fragment, "html.parser")
    for cell in soup.find_all("cell"):
        span = cell.get("span")
        cell.name = "th" if cell.get("role") == "head" else "td"
        cell.attrs = {"colspan": span} if span else {}
    for row in soup.find_all("row"):
        row.name = "tr"
        row.attrs = {}
    return str(soup)


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    result = trafilatura.extract(html, url=url, output_format="html",
                                 include_comments=False, include_tables=True,
                                 include_links=True, include_images=True,
                                 favor_precision=True)
    if not result:
        return None
    return _to_html_tags(result)


_META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']refresh["'][^>]*\bcontent\s*=\s*["'][^"']*?"""
    r"""url\s*=\s*([^"'\s;]+)""",
    re.IGNORECASE,
)
_META_REFRESH_ALT_RE = re.compile(
    r"""<meta[^>]+\bcontent\s*=\s*["'][^"']*?url\s*=\s*([^"'\s;]+)["'][^>]*"""
    r"""http-equiv\s*=\s*["']refresh["']""",
    re.IGNORECASE,
)
# A redirect stub is a sentence and a link. Anything with real text is a page that
# happens to carry a refresh — a slideshow, a live blog — and following it would
# replace an article we already have.
_META_REFRESH_MAX_WORDS = 100


def _meta_refresh_target(html: str, base_url: str) -> Optional[str]:
    """The address a client-side redirect page is pointing at, if that is all it is.

    Some sites answer an old address with a stub that redirects in the browser rather
    than over HTTP: ``blog.rust-lang.org`` serves a 460-byte page holding a script, a
    ``<meta http-equiv="refresh">`` in a ``<noscript>``, and the words "Click here to
    be redirected". We follow HTTP redirects and stop at that, so saving such a link
    stored the stub — the article's whole text became "Click here to be redirected".

    Restricted to the same host, and to a page with almost no text of its own. A
    cross-host client-side redirect is the shape a tracker or an interstitial takes,
    and following one would let a page hand us an article that is not the one asked
    for; a genuine cross-host move is served over HTTP, which is already followed.
    """
    # Searched over the whole document rather than the head: the tag is routinely
    # parked in a <noscript> down in the body, which is where the Rust stub keeps it,
    # and that stub has no </head> to slice on at all. The regex is the cheap half of
    # this function, so it runs before the text is counted.
    m = _META_REFRESH_RE.search(html) or _META_REFRESH_ALT_RE.search(html)
    if not m:
        return None
    if len(nh3.clean(html, tags=set()).split()) > _META_REFRESH_MAX_WORDS:
        return None
    target = urljoin(base_url, html_mod.unescape(m.group(1).strip()))
    if not target.startswith(("http://", "https://")) or target == base_url:
        return None
    return target if _same_host(target, base_url) else None


_HEADING_RE = re.compile(r"<h[1-4]\b", re.IGNORECASE)

# How many headings readability has to find before its result is preferred, and how
# many the page must carry for the question to be worth asking at all. Four is high
# enough that a stray heading in a nav bar cannot reach it.
_MIN_FALLBACK_HEADINGS = 4


def _heading_count(html: Optional[str]) -> int:
    return len(_HEADING_RE.findall(html or ""))


def _prefer_readability(content: str, page_html: str) -> bool:
    """True when trafilatura flattened a structured page and readability did not.

    Some documents come back from trafilatura as unbroken prose with no headings at
    all. On a reference page that is not a style, it is damage: a man page, the Rust
    book, the Kubernetes docs and Pride and Prejudice all arrived as one wall of text,
    and on PostgreSQL's SQL reference it goes further, because trafilatura renders an
    inline ``<code>`` as a block-level ``<pre>`` and every sentence mentioning a
    keyword is cut into three pieces. readability keeps both the headings and the
    inline markup on exactly these pages.

    The rule stays this narrow because trafilatura is the better extractor nearly
    everywhere and must not be displaced on a guess. Measured against Zyte's
    article-extraction-benchmark — 181 pages with a hand-written ground truth, scored
    with that project's own token F1 — trafilatura alone gets 0.959 and readability
    alone 0.922, so switching by default would cost real accuracy. This check fires on
    **none** of those 181 pages, and on 7 of 7 of the reference pages that prompted it.

    A version of this that also switched on the shredded-paragraph shape was measured
    and dropped: it fired once on the benchmark and took F1 down to 0.956.
    """
    return _heading_count(content) == 0 and _heading_count(page_html) >= _MIN_FALLBACK_HEADINGS


_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def _repair_headings(html: str) -> str:
    """Give trafilatura a second look at a page whose headings it threw away.

    Two things in here, both of which turn a heading trafilatura discards into one it
    keeps, and neither of which changes what the page says.

    The permalink anchor is the one that prompted this. GitHub hangs an
    ``<a class="anchor">`` holding nothing but an SVG link icon beside every heading in
    a README, and that is enough for trafilatura to drop the heading: astral-sh/uv came
    back with 0 of its 18, so the README arrived as one wall of text. It is the same
    shape as MediaWiki's ``[edit]`` link, which ``_lift_mediawiki_chrome`` removes for
    exactly this reason. Only anchors with no text of their own go, and only next to or
    inside a heading, so an ordinary link in a title is left alone.

    Reserializing through BeautifulSoup is the second, and it is why the whole document
    goes through here rather than only the anchors. Some pages are simply malformed:
    danluu.com loses all 6 of its headings to trafilatura and gets them back from a
    parse-and-print round trip alone, no anchors involved.
    """
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all(_HEADING_TAGS):
        for anchor in heading.find_all("a"):
            if not anchor.get_text(strip=True):
                anchor.decompose()
        for sibling in (heading.find_previous_sibling(), heading.find_next_sibling()):
            if sibling is not None and sibling.name == "a" and not sibling.get_text(strip=True):
                sibling.decompose()
    return str(soup)


def _extract_with_readability(html: str) -> Optional[str]:
    try:
        doc = Document(html)
        content = doc.summary(html_partial=True)
        return content if content and len(content.strip()) > 50 else None
    except Exception:
        return None


def _sanitize(html: str) -> str:
    allowed_tags = {
        "a", "abbr", "article", "b", "blockquote", "br", "caption", "cite", "code",
        "col", "colgroup", "dd", "del", "details", "dfn", "div", "dl", "dt", "em",
        "figcaption", "figure", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i",
        "img", "ins", "kbd", "li", "mark", "ol", "p", "pre", "q", "rp", "rt",
        "ruby", "s", "samp", "section", "small", "span", "strong", "sub", "summary",
        "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr", "u",
        "ul", "var",
    }
    allowed_attrs = {
        "a": {"href", "title"},
        # The marks a lifted infobox is rendered by (see _lift_mediawiki_chrome).
        # data-infobox is what the stylesheet floats the box on, and `open` decides
        # whether it starts expanded; without both in the allowlist every box would
        # come out collapsed and styled as an ordinary table.
        "details": {"open", "data-infobox"},
        # Kept so a stored video survives as something a player can be built from
        # (see app.utils.video). Both are inert ids, not URLs, and whatever reads them
        # has to validate them anyway, since a feed's own markup passes through here.
        "figure": {"data-video-provider", "data-video-id"},
        "img": {"src", "alt", "title", "width", "height"},
        "td": {"colspan", "rowspan"},
        "th": {"colspan", "rowspan", "scope"},
        "col": {"span"},
        "colgroup": {"span"},
        "time": {"datetime"},
    }
    return nh3.clean(html, tags=allowed_tags, attributes=allowed_attrs,
                     link_rel="noopener noreferrer")


# Block elements left empty after sanitization (e.g. an `<li>` whose only child was a
# stripped share-button link) render as stray bullets/gaps. Media-bearing blocks are kept.
_EMPTY_BLOCK_TAGS = ["li", "p"]
_MEDIA_TAGS = ["img", "picture", "video", "audio", "iframe", "svg", "source"]


def _has_visible_content(html: str) -> bool:
    """True if sanitized HTML has real text or media — not just whitespace.

    Some pages (e.g. Reddit comment threads) extract to markup that collapses to
    pure whitespace after sanitization; such results must not be stored as success.
    """
    if not html or not html.strip():
        return False
    soup = BeautifulSoup(html, "html.parser")
    if soup.get_text(strip=True):
        return True
    return soup.find(_MEDIA_TAGS) is not None


def _dedupe_images(html: str) -> str:
    """
    Drop repeated images that point at the same file. News CMSs emit the same photo
    several times (lead + inline + responsive <picture> renditions), and trafilatura
    extracts each one, so the readable body shows the same picture two or three times
    (seen on aktualne.cz / economia). Keys on the URL filename — not the full src — so
    different upload paths of one file (…/47/48/<uuid>/foo.jpg vs …/47/37/<uuid>/foo.jpg)
    collapse too. Keeps the first occurrence.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    for img in soup.find_all("img"):
        key = urlsplit(img.get("src") or "").path.rsplit("/", 1)[-1].lower()
        if not key:
            continue
        if key in seen:
            img.decompose()
        else:
            seen.add(key)
    return str(soup)


def _is_tracking_pixel(img) -> bool:
    """A 1x1 (or 0x0) image, which feeds carry for counting reads, not for looking at."""
    return all((img.get(dim) or "").strip().rstrip("px") in ("0", "1")
               for dim in ("width", "height"))


def _carry_over_feed_media(article, content: str) -> str:
    """Put back pictures the feed delivered and the extraction did not.

    A webcomic is the case that shows why. xkcd's feed hands over the strip itself,
    one ``<img>`` carrying the picture, the alt text and the hover joke, and nothing
    else at all; the comic *is* the article. Extraction of the page finds none of it
    and returns the site's footer gag about Netscape Navigator, and since a successful
    extraction takes precedence over feed content when the article is rendered, an
    xkcd subscriber would have been shown the footer instead of the comic.

    Carrying the pictures over rather than rejecting the extraction outright is the
    deliberate choice here, and the first design was the other one. Rejecting needs a
    rule for when the feed's version is the better of the two, and there is no good
    one: a news item whose teaser carries the lead photo would match a
    "feed has pictures, extraction has none" test just as squarely as the comic does,
    and answering it by keeping the teaser would throw away the whole article to save
    one photograph. Merging has no such threshold to get wrong. Nothing is discarded,
    the comic reappears, and a news article keeps every word *and* regains its lead
    photo.

    Only ever runs when the extraction came back with no media whatsoever, so an
    article that kept its own pictures is left exactly as it was.
    """
    feed_html = getattr(article, "content", None)
    if not feed_html or not feed_html.strip():
        return content
    if BeautifulSoup(content, "html.parser").find(_MEDIA_TAGS):
        return content
    feed_soup = BeautifulSoup(feed_html, "html.parser")
    carried = [str(img) for img in feed_soup.find_all("img")
               if img.get("src") and not _is_tracking_pixel(img)]
    if not carried:
        return content
    logger.info("readable: carried %d image(s) from feed content for article %s",
                len(carried), getattr(article, "id", "?"))
    # Feed content was sanitized on the way in, but with the fetcher's allowlist
    # rather than this module's, and readable_content is rendered unescaped.
    return _sanitize("".join(carried)) + content


def _drop_empty_blocks(html: str) -> str:
    """Remove block elements with no text and no media, left empty by sanitization."""
    soup = BeautifulSoup(html, "html.parser")
    # Repeat until stable: removing an inner empty block can empty its parent.
    while True:
        removed = False
        for el in soup.find_all(_EMPTY_BLOCK_TAGS):
            if el.get_text(strip=True) or el.find(_MEDIA_TAGS):
                continue
            el.decompose()
            removed = True
        if not removed:
            break
    return str(soup)


def _find_published_date(html: str, url: str) -> Optional[datetime]:
    """
    Best-effort publication date scraped from the article page via htmldate.
    Used to backfill Article.published_at for feeds whose listing carries no date
    (e.g. sites without <time datetime> in their cards). htmldate is date-granular
    — it discards time-of-day — so the result is that date at midnight UTC.
    Returns None when no date is found or it parses implausibly.
    """
    try:
        from htmldate import find_date
        # original_date=False (htmldate's default) leans on the page's own metadata
        # (JSON-LD datePublished, meta tags) for the primary article date. Do NOT use
        # original_date=True here: it favours the *oldest* date anywhere on the page,
        # which on news sites grabs a related-article link's date (seen on denik.cz:
        # a 2024/2026 mismatch) instead of the article's own publication date.
        raw = find_date(html, url=url, original_date=False)  # default format '%Y-%m-%d'
    except Exception as exc:
        logger.debug("htmldate find_date failed for %s: %s", url, exc)
        return None
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    # Reject implausible future dates (clock skew / malformed markup) that would
    # otherwise sort to the top of the reading list.
    if dt > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return dt


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
    * the page does not claim to be the article. A document viewer legitimately built
      around ``?url=`` says so with rel=canonical or og:url, and is waved through, the
      same escape hatch cross-host redirects already get.
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
    # The page naming the requested article as its own address is the article.
    declared = _declared_url(fetched_url, html)
    return not (declared and declared != fetched_url)


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
    if not text or len(text.split()) > _BOT_WALL_MAX_WORDS:
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


def title_from_url(url: str) -> str:
    """Readable stand-in when the page carries no title at all.

    Host + path beats showing the raw URL in a list, and beats an empty title —
    Article.title is NOT NULL.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:1000]
    host = (parts.netloc or "").removeprefix("www.")
    path = (parts.path or "").rstrip("/")
    label = f"{host}{path}" if host else url
    return (label or url)[:1000]


def apply_readable_result(
    article: Article,
    content: Optional[str],
    error: Optional[str],
    http_status: Optional[int],
    published_at: Optional[datetime] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> bool:
    """Apply extraction result to article fields. Returns True if HTTP 403."""
    # The page's own og:description, kept for feedless articles only. It is what the
    # reader falls back to when extraction produced nothing usable, and it is also
    # the only thing a saved article can show as a list snippet — _make_snippet reads
    # summary and content, never readable_content, so without this a saved article
    # has no preview at all. Feed articles are left alone: nothing writes summary for
    # them today, and starting to would change snippets and search across every feed.
    if description and article.feed_id is None:
        article.summary = description[:2000]
    # A feedless article (saved by URL) has no other source of a title — nothing
    # arrived over RSS — so the page's own title always wins, even on a failed
    # extraction where it is the only thing left to identify the article by. Feed
    # articles keep their feed-supplied title. Note this is NOT gated on "the title
    # still looks like the placeholder": that would tie this function to whatever
    # placeholder the insert happens to use today.
    if title and article.feed_id is None:
        article.title = title[:1000]

    # Whitespace-only content is treated as no content: storing it would mark the
    # article "success" yet render blank, hiding the (often fuller) feed content.
    if content and content.strip():
        content = _carry_over_feed_media(article, content)
        article.readable_content = content
        article.readable_status = "success"
        article.readable_error = None
        words = count_words(content)
        article.word_count = words
        article.estimated_read_min = max(1, round(words / 200))
        # Backfill the publication date from the article page only when the feed
        # listing gave us none — never override a date we already trust.
        if published_at is not None and article.published_at is None:
            article.published_at = published_at
        return False

    article.readable_error = error
    is_4xx = http_status is not None and 400 <= http_status < 500
    is_403 = http_status == 403
    # A consent wall answers the same way every time, so scheduled retries would only
    # ask a site that refuses us three times instead of once. Terminal like a 4xx; the
    # Retry button on a saved article still works, and that one is a person's decision.
    # An oversized page is terminal for the same reason, and more sharply: each retry
    # would download the cap again before giving up in exactly the same place. A browser
    # check is the most terminal of the three: it is asking for a JavaScript engine we
    # will not have on the next attempt either.
    if is_4xx or error in (_WRONG_CONTENT_MSG, _TOO_LARGE_MSG, _BOT_WALL_MSG):
        article.readable_status = "failed"
        article.readable_failed_at = datetime.now(timezone.utc)
        article.readable_next_retry_at = None
    else:
        retries = (article.readable_retries or 0) + 1
        article.readable_retries = retries
        if retries >= MAX_RETRIES:
            article.readable_status = "failed"
            article.readable_failed_at = datetime.now(timezone.utc)
            article.readable_next_retry_at = None
        else:
            delay_min = BACKOFF_MINUTES[min(retries - 1, len(BACKOFF_MINUTES) - 1)]
            article.readable_next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=delay_min)
    return is_403


class ReadableResult(NamedTuple):
    """Everything one extraction attempt learned about a page.

    A plain tuple ran out of room: callers need the body, why it failed, the page's
    own title and description, and the address the fetch really ended at. Named
    fields keep the failure paths — which return most of these with no content —
    readable at the call site.
    """
    content: Optional[str] = None
    error: Optional[str] = None
    http_status: Optional[int] = None
    published_at: Optional[datetime] = None
    title: Optional[str] = None
    resolved_url: Optional[str] = None
    description: Optional[str] = None


def extract_readable_with_title(
    url: str, auth_user: Optional[str] = None, auth_pass: Optional[str] = None,
    reject_wrong_content: bool = False,
) -> ReadableResult:
    """
    Download URL and extract readable HTML, plus the page's own title, description and
    the address the article really lives at.

    The title is returned from the failed-extraction paths too, not just on success:
    when a page downloads but yields no article body, its title is the only thing left
    to identify it by in the Saved list.

    *reject_wrong_content* turns on the consent/paywall-page check (see
    ``_content_contradicts_page``). It is off by default and enabled only for articles
    with no feed: a feed article that trips the heuristic would lose content it has
    been showing fine, whereas a saved one has nothing to lose and an honest error
    beats a body made of advertising copy.

    The round-trip check below is not behind that flag. It reads the redirect chain
    rather than the prose, so a feed article cannot lose a body over a wording it
    happens to share with a cookie notice, and a feed whose pages answer a server-side
    fetch with a consent wall would otherwise store that wall for every article.
    """
    html, fetch_error, http_status, final_url = _fetch_html(url, auth_user, auth_pass)
    if not html:
        # Nothing was downloaded, so there is no title or address to report either.
        return ReadableResult(error=fetch_error, http_status=http_status)

    # A client-side redirect stub, followed once. Not in a loop: two stubs in a row is
    # not a thing a real site does, and a loop here is a way to be walked in circles.
    hop = _meta_refresh_target(html, final_url or url)
    if hop:
        hop_html, _, _, hop_final = _fetch_html(hop, auth_user, auth_pass)
        if hop_html:
            logger.info("readable: followed a meta refresh from %s to %s", url, hop)
            html, final_url = hop_html, hop_final or hop

    if redirected_back_to_us(final_url, url, html):
        # An interstitial holding the address it interrupted. Report it as the wrong
        # page rather than extracting it, and keep the requested address: adopting the
        # wall's own URL would make "Open original" and Retry walk back into it.
        # The wall's title and description are not withheld by oversight: they describe
        # the wall ("iDNES.cz – s námi víte víc", the site's generic blurb), and a saved
        # article takes both from the page, so reporting them would file the interstitial
        # under its own name. With neither, the row keeps the address it was saved from.
        logger.info("readable: fetch of %s was answered by %s", url, final_url)
        return ReadableResult(error=_WRONG_CONTENT_MSG, resolved_url=url)

    title = _extract_title(html)
    # Read off the untouched document: _strip_pre_extraction_noise below rewrites the
    # markup, and both of these live in <head>.
    resolved_url = resolve_article_url(final_url, html, url)
    description = _extract_og_description(html)

    video = video_target(final_url) or video_target(url)
    if video:
        # A watch page holds no prose to extract. Its description is drawn by the
        # page's own JavaScript, so the extractor finds nothing but the site footer
        # ("About, Press, Copyright, Contact us"), which is both worthless as an
        # article and — having nothing in common with the description — read as a
        # substitute page by _content_contradicts_page below, so saving a video link
        # failed with a consent-wall error on a page that had answered perfectly.
        # The video and its description are what the page is, so that is what is
        # stored, and neither the extractor nor that check gets a say.
        provider, vid = video
        return ReadableResult(
            content=video_page_content(provider, vid, html, description),
            published_at=_find_published_date(html, url), title=title,
            resolved_url=resolved_url, description=description,
        )

    video_figures = collect_video_figures(html)
    wiki = _lift_mediawiki_chrome(html)
    html = _strip_pre_extraction_noise(wiki.html)
    content = _extract_with_trafilatura(html, url)
    if not content:
        content = _extract_with_readability(html)
    elif _prefer_readability(content, html):
        # Gated on the cheap heading counts first, so neither the repair nor readability
        # is built for pages that do not need them rather than for every article.
        # Trafilatura gets the first of the two: it is the better extractor of the pair
        # everywhere else, so where repairing the page is enough to bring the headings
        # back there is no reason to hand the article to readability instead.
        retry = _extract_with_trafilatura(_repair_headings(html), url)
        if _heading_count(retry) >= _MIN_FALLBACK_HEADINGS:
            logger.info("readable: re-read %s with its heading permalinks removed"
                        " (trafilatura returned no headings on a structured page)", url)
            content = retry
        else:
            alternative = _extract_with_readability(html)
            if _heading_count(alternative) >= _MIN_FALLBACK_HEADINGS:
                logger.info("readable: using readability for %s (trafilatura returned no"
                            " headings on a structured page)", url)
                content = alternative
    if not content:
        # An infobox on its own is not an article, so a page that yielded nothing else
        # fails here as it always did rather than being stored as a lone table.
        logger.warning("readable extraction yielded no content for %s", url)
        return ReadableResult(error=_EMPTY_CONTENT_MSG, title=title,
                              resolved_url=resolved_url, description=description)

    if video_figures:
        content += "\n" + "\n".join(video_figures)
    body = _drop_empty_blocks(_dedupe_images(_sanitize(content)))
    # Lifted markup is sanitized on its own and joined on afterwards, deliberately
    # skipping _dedupe_images: that keys on the filename, and a MediaWiki table repeats
    # one file on purpose — Battle of Waterloo's infobox carries 54 images that are 8
    # flags, one per unit — so running it here would leave most rows with none.
    if wiki.tables:
        body = _restore_wiki_tables(body, wiki.tables)
    if wiki.infoboxes:
        body = "".join(_sanitize(box) for box in wiki.infoboxes) + body
    final = rewrite_relative_urls(soften_nbsp_runs(body), url)
    if not _has_visible_content(final):
        # Extraction produced markup that sanitized down to nothing usable.
        logger.warning("readable extraction collapsed to empty content for %s", url)
        return ReadableResult(error=_EMPTY_CONTENT_MSG, title=title,
                              resolved_url=resolved_url, description=description)
    if reject_wrong_content and _content_contradicts_page(final, description):
        # HTTP 200 with a consent/paywall page in place of the article. Discarding it
        # surfaces the error and the "Open original" / "Retry" buttons instead of
        # storing the site's cookie notice as the article body.
        logger.info("readable extraction returned a substitute page for %s", url)
        return ReadableResult(error=_WRONG_CONTENT_MSG, title=title,
                              resolved_url=resolved_url, description=description)
    if reject_wrong_content and _looks_like_a_bot_wall(final):
        # A browser check, which the description test above cannot see because a wall
        # ships without a description to compare against.
        logger.info("readable extraction returned a browser check for %s", url)
        return ReadableResult(error=_BOT_WALL_MSG, title=title,
                              resolved_url=resolved_url, description=description)
    return ReadableResult(
        content=final, published_at=_find_published_date(html, url), title=title,
        resolved_url=resolved_url, description=description,
    )


def extract_readable(url: str, auth_user: Optional[str] = None,
                     auth_pass: Optional[str] = None) -> tuple[Optional[str], Optional[str], Optional[int], Optional[datetime]]:
    """
    Download URL and extract readable HTML.
    Returns (sanitized HTML, error_message, http_status_code, published_at). On success,
    the first element is set and published_at may carry a date scraped from the page.

    Feed articles already have a title, so this drops the one
    ``extract_readable_with_title`` collects rather than making every caller unpack it.
    """
    r = extract_readable_with_title(url, auth_user, auth_pass)
    return r.content, r.error, r.http_status, r.published_at


# ── scheduler job ─────────────────────────────────────────────────────────────

async def _extract_for_batch(article: Article, auth, loop) -> ReadableResult:
    """One extraction for the batch worker, off the event loop, never raising.

    Both kinds of article come back as a ReadableResult so the loop has one shape to
    work with. A saved-by-URL article asks for the fuller extraction: it has no feed
    title to fall back on, so it needs the page's own title and description, the
    address the fetch really ended at, and the consent/paywall check that a feed
    article deliberately does not get (see extract_readable_with_title).

    A crash becomes a failed result rather than an exception, because one unlucky
    page must not take the rest of the batch with it.
    """
    auth_user, auth_pass = auth or (None, None)
    try:
        if article.feed_id is None:
            return await loop.run_in_executor(
                None, extract_readable_with_title, article.url, auth_user, auth_pass, True
            )
        content, error, http_status, published_at = await loop.run_in_executor(
            None, extract_readable, article.url, auth_user, auth_pass
        )
        return ReadableResult(
            content=content, error=error, http_status=http_status, published_at=published_at
        )
    except Exception as exc:
        logger.warning("readable extraction error for article %d: %s", article.id, exc)
        return ReadableResult(error=str(exc)[:200])


async def store_saved_extraction(
    article: Article, result: ReadableResult, db: AsyncSession
) -> None:
    """Write a saved-by-URL extraction and run its post-processing. Commits.

    Every way an extraction can finish outside the import task ends here: the batch
    worker picking up an article whose import task died or hit a transient error, and
    the Retry button doing the same by hand. Without the post-processing such an
    article comes out fully extracted and then silently never filtered, and writing
    the steps out at each entry point is how one of them came to miss it. Filters are
    per-saver and there is no scoring, which is what keeps this apart from the feed
    path.
    """
    from app.services.saved_article_service import (
        adopt_resolved_url, finalize_for_all_savers,
    )

    apply_readable_result(
        article, result.content, result.error, result.http_status, result.published_at,
        title=result.title, description=result.description,
    )
    adopt_resolved_url(article, result.resolved_url)
    if result.content or article.readable_status == "failed":
        await finalize_for_all_savers(article, db)
    await db.commit()


async def process_pending_readable(db: AsyncSession) -> int:
    """
    Process a batch of articles with readable_status='pending'.
    Returns the number of articles processed.
    """
    result = await db.execute(
        select(Article)
        .where(
            Article.readable_status == "pending",
            # never re-extract a retention-trimmed stub — it would re-fetch the body
            Article.trimmed_at.is_(None),
            Article.url.isnot(None),
            Article.url != "",
            and_(
                Article.readable_next_retry_at.is_(None)
                | (Article.readable_next_retry_at <= datetime.now(timezone.utc))
            ),
        )
        .order_by(Article.id)
        .limit(_BATCH_SIZE)
    )
    articles = result.scalars().all()
    if not articles:
        return 0

    # Load feed auth info for articles that need it. Saved-by-URL articles have no
    # feed, so drop the None before it reaches the IN clause.
    feed_ids = list({a.feed_id for a in articles if a.feed_id is not None})
    feeds_result = await db.execute(
        select(Feed.id, Feed.fetch_auth_user, Feed.fetch_auth_pass_encrypted)
        .where(Feed.id.in_(feed_ids))
    )
    auth_by_feed: dict[int, tuple[str, str] | None] = {
        feed_id: feed_auth(auth_user, auth_pass_enc, context=f"feed {feed_id}")
        for feed_id, auth_user, auth_pass_enc in feeds_result
    }

    import asyncio
    loop = asyncio.get_running_loop()

    processed = 0
    feed_403_streak: dict[int, int] = {}     # consecutive 403s per feed in this batch
    feeds_to_disable: set[int] = set()       # feeds that hit the 403 threshold
    feeds_with_403: set[int] = set()         # feeds with any 403 (cross-batch check)
    feed_empty_streak: dict[int, int] = {}   # consecutive empty extractions per feed
    feeds_to_disable_empty: set[int] = set() # feeds that hit the empty threshold
    feeds_with_empty: set[int] = set()       # feeds with any empty (cross-batch check)

    for article in articles:
        # Feed hit a disable threshold earlier in this batch — skip without fetching.
        # Leave it 'pending' (don't mark skipped here): the disable step below cancels
        # *and* runs the AI pipeline on every still-pending article for the feed, so
        # marking it skipped now would orphan it from scoring/filters.
        if article.feed_id in feeds_to_disable or article.feed_id in feeds_to_disable_empty:
            processed += 1
            continue

        result = await _extract_for_batch(
            article, auth_by_feed.get(article.feed_id), loop
        )

        # Re-check status — on-demand extraction may have already processed this article
        await db.refresh(article)
        if article.readable_status == "success":
            processed += 1
            continue

        # Articles saved by URL have no feed, and nothing below this point can serve
        # them: every one would land in the same `None` bucket of the per-feed
        # bookkeeping, so unrelated hosts would pool their 403s and empties and could
        # trip _disable_readable_for_403(None, db) for a feed that does not exist.
        if article.feed_id is None:
            await store_saved_extraction(article, result, db)
            processed += 1
            continue

        content = result.content
        is_403 = apply_readable_result(
            article, content, result.error, result.http_status, result.published_at,
        )
        is_empty = content is None and result.error == _EMPTY_CONTENT_MSG
        from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
        if content:
            feed_403_streak.pop(article.feed_id, None)  # reset streaks on success
            feed_empty_streak.pop(article.feed_id, None)
            await run_pipeline_for_article_all_users(article, db)
        elif article.readable_status == "failed":
            # Terminal failure — score with RSS content
            await run_pipeline_for_article_all_users(article, db)
        if is_403:
            streak = feed_403_streak.get(article.feed_id, 0) + 1
            feed_403_streak[article.feed_id] = streak
            feeds_with_403.add(article.feed_id)
            if streak >= _CONSECUTIVE_403_THRESHOLD:
                feeds_to_disable.add(article.feed_id)

        if is_empty:
            streak = feed_empty_streak.get(article.feed_id, 0) + 1
            feed_empty_streak[article.feed_id] = streak
            feeds_with_empty.add(article.feed_id)
            if streak >= _CONSECUTIVE_EMPTY_THRESHOLD:
                feeds_to_disable_empty.add(article.feed_id)
        else:
            # Any non-empty outcome breaks the consecutive-empty streak
            feed_empty_streak.pop(article.feed_id, None)

        processed += 1
        await db.commit()  # per-article: keeps transactions short even with inline AI calls

    # Feeds that hit a threshold within this batch — disable immediately
    for feed_id in feeds_to_disable:
        await _disable_readable_for_403(feed_id, db)
    for feed_id in feeds_to_disable_empty - feeds_to_disable:
        await _disable_readable_for_empty(feed_id, db)

    # Feeds below the in-batch threshold — check cross-batch consecutive counts
    for feed_id in feeds_with_403 - feeds_to_disable:
        await _maybe_disable_readable_for_403(feed_id, db)
    for feed_id in feeds_with_empty - feeds_to_disable_empty - feeds_to_disable:
        await _maybe_disable_readable_for_empty(feed_id, db)

    logger.info("readable: processed %d articles", processed)
    return processed


# ── auto-detection of full-content feeds ─────────────────────────────────────

class FullContentSample(NamedTuple):
    """How many of a feed's recent bodies are whole articles rather than teasers."""
    full: int
    total: int

    @property
    def is_full_content(self) -> bool:
        """True when the feed delivers whole articles often enough to call it that."""
        return bool(self.total) and self.full / self.total >= _FULL_CONTENT_THRESHOLD


async def sample_feed_content(
    feed_id: int, db: AsyncSession, limit: int = _FULL_CONTENT_SAMPLE
) -> FullContentSample:
    """Measure how much text *feed_id* delivers by itself, over its *limit* newest rows.

    Asked from two places, and it has to answer both the same way or a feed would be
    told it delivers full content while it is being subscribed to and the opposite on
    the next fetch: ``maybe_disable_readable_for_feed`` below, and ``services.feed``
    when a second user subscribes to a feed already in the database. They differ only
    in how large a sample they can wait for, hence *limit*, and in what they do with a
    short one, hence the raw counts in the return value.

    Counted from Article.content, the body as it arrived in the feed, and deliberately
    not read from Article.word_count: a successful extraction overwrites that column
    with the word count of the *extracted page* (see apply_readable_result), so a feed
    whose extraction works well would read as a full-content feed and turn its own
    extraction off. Trimmed articles are left out because retention has already
    dropped or replaced their body.
    """
    result = await db.execute(
        # Only the head of each body travels: the question is whether it clears
        # _FULL_CONTENT_MIN_WORDS, and _FULL_CONTENT_SAMPLE_CHARS is far more room
        # than that needs, so the sample costs the same on a feed of 500-word posts
        # as on one of essays.
        select(func.substr(Article.content, 1, _FULL_CONTENT_SAMPLE_CHARS))
        .where(
            Article.feed_id == feed_id,
            Article.content.isnot(None),
            Article.content != "",  # sanitizer emptied it: no body to measure
            Article.trimmed_at.is_(None),
        )
        .order_by(Article.id.desc())
        .limit(limit)
    )
    counts = [count_words(row[0]) for row in result]
    return FullContentSample(
        full=sum(1 for c in counts if c > _FULL_CONTENT_MIN_WORDS),
        total=len(counts),
    )


async def maybe_disable_readable_for_feed(feed_id: int, db: AsyncSession) -> bool:
    """
    Check if a feed consistently delivers full content by itself.
    If so, disable extract_readable on all UserFeed rows for this feed.
    Returns True if disabled.

    Runs on every fetch that brings new articles, so it asks who would be affected
    before it measures anything. On a feed nobody extracts, which includes every feed
    this check has already disabled, the answer changes nothing, and those are exactly
    the feeds with the largest bodies to read.

    Unlike the same measurement at subscribe time, this one insists on a full sample:
    turning extraction off for everyone is not a decision to make on three articles.
    """
    user_feeds_result = await db.execute(
        select(UserFeed).where(
            UserFeed.feed_id == feed_id,
            UserFeed.extract_readable == True,
        )
    )
    user_feeds = user_feeds_result.scalars().all()
    if not user_feeds:
        return False

    sample = await sample_feed_content(feed_id, db)
    if sample.total < _FULL_CONTENT_SAMPLE:
        return False  # not enough data yet
    if not sample.is_full_content:
        return False

    for uf in user_feeds:
        uf.extract_readable = False
        uf.readable_auto_disabled = True
        uf.readable_auto_disabled_reason = "full_content"
    await db.commit()

    # Mark pending articles for this feed as skipped (no need to extract)
    pending_result = await db.execute(
        select(Article).where(
            Article.feed_id == feed_id,
            Article.readable_status == "pending",
        )
    )
    pending = pending_result.scalars().all()
    for article in pending:
        article.readable_status = "skipped"
    if pending:
        await db.commit()

    logger.info(
        "readable: auto-disabled extraction for feed %d (%d/%d articles have full content)",
        feed_id, sample.full, sample.total,
    )
    return True


_CONSECUTIVE_403_THRESHOLD = 3
# Empty extractions are a weaker signal than 403s (an odd article can extract to
# nothing on an otherwise-good feed), so require a longer streak before disabling.
_CONSECUTIVE_EMPTY_THRESHOLD = 5

# Days before a feed disabled for 403s is probed again, indexed by attempts already
# spent. Two tries for the feed's whole lifetime: the first catches a fix on our side
# (the switch to HTTP/2 was exactly that), the second a change on theirs. A site still
# refusing after a fortnight is refusing on purpose.
_REVIVAL_BACKOFF_DAYS = [3, 14]
_REVIVAL_BATCH_SIZE = 10  # feeds probed per scheduler run


async def stamp_readable_streak_start(feed_id: int, db: AsyncSession) -> None:
    """Record where the streak checks may start counting for a feed.

    Called wherever readable extraction comes back on, by hand or by a revival probe.
    Disabling leaves articles as they are, so without this the failures that got the
    feed disabled stay its newest terminal rows and the very next one re-trips the
    check — a threshold of 3 or 5 collapsing to 1.

    Stores the feed's newest article id rather than a timestamp: a success has to be
    able to break a streak, and articles only carry readable_failed_at, so a time-based
    window would silently drop the successes out of the middle of it. Articles that
    arrived while extraction was off are 'skipped' and never counted anyway.

    Does not commit — every caller writes more than this.
    """
    newest = await db.scalar(
        select(func.max(Article.id)).where(Article.feed_id == feed_id)
    )
    feed = await db.get(Feed, feed_id)
    if feed is not None:
        feed.readable_streak_from_id = newest


def _defer_revival(feed: Feed, now: datetime) -> None:
    """Point a feed at its next revival probe, or stop probing for good.

    Reads Feed.readable_revival_attempts, which counts every probe the feed has been
    given over its whole lifetime, passing ones included, and is never reset by this
    module. Both halves of that matter. A 403 is usually per-IP or rate-based rather
    than per-URL, so a probe can pass while the feed is still blocked: extraction comes
    back on and users start collecting 403s again. The streak check no longer counts the
    old ones (see stamp_readable_streak_start), so the feed gets a fair three articles
    before it goes off again, but go off it will. If a passing probe were free, or the
    counter reset on re-disable, that would repeat every few days forever. Charging for
    it lets the loop end by itself.
    """
    attempts = feed.readable_revival_attempts or 0
    if attempts >= len(_REVIVAL_BACKOFF_DAYS):
        feed.readable_revival_next_at = None
    else:
        feed.readable_revival_next_at = now + timedelta(days=_REVIVAL_BACKOFF_DAYS[attempts])


async def _disable_readable_for_feed(
    feed_id: int, db: AsyncSession, *, pending_error: str
) -> Optional[int]:
    """Turn off readable extraction for a feed and cancel its pending articles.

    Returns the number of pending articles cancelled, or None if the feed had no
    active subscribers to disable (nothing happened).
    """
    user_feeds_result = await db.execute(
        select(UserFeed).where(
            UserFeed.feed_id == feed_id,
            UserFeed.extract_readable == True,
        )
    )
    user_feeds = user_feeds_result.scalars().all()
    if not user_feeds:
        return None

    for uf in user_feeds:
        uf.extract_readable = False
        uf.readable_auto_disabled = True
        uf.readable_auto_disabled_reason = "blocked"

    pending_result = await db.execute(
        select(Article).where(
            Article.feed_id == feed_id,
            Article.readable_status == "pending",
        )
    )
    pending = pending_result.scalars().all()
    for article in pending:
        article.readable_status = "skipped"
        article.readable_error = pending_error

    await db.commit()

    from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
    for article in pending:
        await run_pipeline_for_article_all_users(article, db)
    return len(pending)


async def _disable_readable_for_403(feed_id: int, db: AsyncSession) -> None:
    """Disable readable extraction for a feed after repeated 403 responses."""
    cancelled = await _disable_readable_for_feed(
        feed_id, db, pending_error="HTTP 403 Forbidden"
    )
    if cancelled is None:
        return
    logger.warning(
        "readable: disabled extraction for feed %d after %d consecutive 403 errors"
        " (cancelled %d pending articles)",
        feed_id, _CONSECUTIVE_403_THRESHOLD, cancelled,
    )
    # Only 403s get a revival probe. An empty extraction means the page downloaded
    # fine and simply held nothing we could use, which waiting a fortnight will not
    # change, so _disable_readable_for_empty deliberately schedules nothing.
    feed = await db.get(Feed, feed_id)
    if feed is not None:
        _defer_revival(feed, datetime.now(timezone.utc))
        await db.commit()


async def _disable_readable_for_empty(feed_id: int, db: AsyncSession) -> None:
    """Disable readable extraction for a feed that consistently extracts nothing."""
    cancelled = await _disable_readable_for_feed(
        feed_id, db, pending_error=_EMPTY_CONTENT_MSG
    )
    if cancelled is None:
        return
    logger.warning(
        "readable: disabled extraction for feed %d after %d consecutive empty"
        " extractions (cancelled %d pending articles)",
        feed_id, _CONSECUTIVE_EMPTY_THRESHOLD, cancelled,
    )


async def _recent_terminal_articles(
    feed_id: int, limit: int, db: AsyncSession
) -> list[tuple[str, Optional[str]]]:
    """The feed's *limit* newest terminal extraction outcomes, newest first.

    Successes are in there with the failures: one of them breaks a streak, which is the
    whole reason the callers look at outcomes rather than at failures alone. Anything at
    or below Feed.readable_streak_from_id is left out, so failures from before the last
    re-enable cannot be counted twice.
    """
    stmt = (
        select(Article.readable_status, Article.readable_error)
        .where(
            Article.feed_id == feed_id,
            Article.readable_status.in_(["failed", "success"]),
        )
        .order_by(Article.id.desc())
        .limit(limit)
    )
    streak_from_id = await db.scalar(
        select(Feed.readable_streak_from_id).where(Feed.id == feed_id)
    )
    if streak_from_id is not None:
        stmt = stmt.where(Article.id > streak_from_id)
    result = await db.execute(stmt)
    return result.all()


async def _maybe_disable_readable_for_403(feed_id: int, db: AsyncSession) -> None:
    """Disable readable if the last N processed articles for the feed all returned 403.

    Used for cross-batch detection: when a feed accumulates 403s across multiple scheduler
    runs (few articles per batch), this catches it once the consecutive count is reached.
    """
    rows = await _recent_terminal_articles(feed_id, _CONSECUTIVE_403_THRESHOLD, db)

    if len(rows) < _CONSECUTIVE_403_THRESHOLD:
        return

    all_403 = all(
        status == "failed" and error and " 403" in error
        for status, error in rows
    )
    if not all_403:
        return

    await _disable_readable_for_403(feed_id, db)


async def _maybe_disable_readable_for_empty(feed_id: int, db: AsyncSession) -> None:
    """Disable readable if the last N terminal articles for the feed all extracted empty.

    Cross-batch counterpart of the in-batch streak check: when empty extractions
    accumulate across scheduler runs (few articles per batch), this catches the feed
    once enough terminally-failed articles share the empty-content error.
    """
    rows = await _recent_terminal_articles(feed_id, _CONSECUTIVE_EMPTY_THRESHOLD, db)

    if len(rows) < _CONSECUTIVE_EMPTY_THRESHOLD:
        return

    all_empty = all(
        status == "failed" and error == _EMPTY_CONTENT_MSG
        for status, error in rows
    )
    if not all_empty:
        return

    await _disable_readable_for_empty(feed_id, db)


# ── revival of feeds disabled for 403s ────────────────────────────────────────

async def _revive_readable_for_feed(feed: Feed, db: AsyncSession) -> int:
    """Turn readable extraction back on for a feed whose block is gone.

    Mirror of _disable_readable_for_feed. Only touches subscribers we disabled
    ourselves: readable_auto_disabled is the marker for "this was us", and saving the
    feed form clears it, so anyone who turned extraction off by hand keeps it off.

    Articles are left untouched. Old ones stay 'skipped' or 'failed'; the point is that
    new articles get extracted again, and that labelled ones reach AI scoring with the
    full text rather than the RSS stub (see filter_service._apply_filters_for_user).

    Spends a revival attempt, so a feed revived by a probe that turned out to be wrong
    is not handed the same number of tries all over again.
    """
    result = await db.execute(
        select(UserFeed).where(
            UserFeed.feed_id == feed.id,
            UserFeed.readable_auto_disabled == True,
            UserFeed.readable_auto_disabled_reason == "blocked",
        )
    )
    user_feeds = result.scalars().all()
    for uf in user_feeds:
        uf.extract_readable = True
        uf.readable_auto_disabled = False
        uf.readable_auto_disabled_reason = None

    # The probe is spent whether or not it told the truth. A 403 is usually per-IP or
    # rate-based, so a probe can pass while the feed is still blocked; counting only the
    # failed ones would let disable → revive → disable repeat every few days forever.
    feed.readable_revival_attempts = (feed.readable_revival_attempts or 0) + 1
    feed.readable_revival_next_at = None
    feed.readable_revived_at = datetime.now(timezone.utc)
    await stamp_readable_streak_start(feed.id, db)
    await db.commit()
    return len(user_feeds)


async def retry_blocked_feeds(db: AsyncSession) -> int:
    """Probe feeds whose readable extraction was auto-disabled for repeated 403s.

    One HTTP request per feed, against its newest article, answering a single question:
    does the host still refuse us? A page that downloads but extracts to nothing counts
    as a pass, because the subject here is the block, not this article's markup — a
    video post or live blog at the top of the feed must not condemn the whole feed.

    Returns the number of feeds revived.
    """
    now = datetime.now(timezone.utc)
    result = await db.execute(
        select(Feed)
        .where(
            Feed.readable_revival_next_at.isnot(None),
            Feed.readable_revival_next_at <= now,
        )
        .order_by(Feed.readable_revival_next_at)  # longest-waiting first, no starvation
        .limit(_REVIVAL_BATCH_SIZE)
    )
    feeds = result.scalars().all()
    if not feeds:
        return 0

    import asyncio
    loop = asyncio.get_running_loop()
    revived = 0

    for feed in feeds:
        still_disabled = await db.scalar(
            select(func.count(UserFeed.id)).where(
                UserFeed.feed_id == feed.id,
                UserFeed.readable_auto_disabled == True,
                UserFeed.readable_auto_disabled_reason == "blocked",
            )
        )
        if not still_disabled:
            # Everyone has since sorted it out by hand; nothing left to revive.
            feed.readable_revival_next_at = None
            await db.commit()
            continue

        article_url = await db.scalar(
            select(Article.url)
            .where(
                Article.feed_id == feed.id,
                Article.url.isnot(None),
                Article.url != "",
                Article.trimmed_at.is_(None),  # a trimmed stub has no page to fetch
            )
            .order_by(Article.id.desc())
            .limit(1)
        )
        if not article_url:
            # Nothing to probe. Spend the attempt anyway: leaving next_at in the past
            # would burn a batch slot every single day, and an empty feed has nothing
            # to offer a later probe either.
            feed.readable_revival_attempts = (feed.readable_revival_attempts or 0) + 1
            _defer_revival(feed, now)
            await db.commit()
            continue

        auth_user, auth_pass = feed_auth(
            feed.fetch_auth_user, feed.fetch_auth_pass_encrypted, context=f"feed {feed.id}"
        ) or (None, None)

        try:
            content, error, http_status, _ = await loop.run_in_executor(
                None, extract_readable, article_url, auth_user, auth_pass
            )
        except Exception as exc:
            content, error, http_status = None, str(exc)[:200], None
            logger.warning("readable revival probe failed for feed %d: %s", feed.id, exc)

        # Pass = the page provably came down. Content is the obvious case; the empty
        # message is the other one, since extraction only runs on a body we received.
        # Everything else (403, but also a timeout or DNS failure) proves nothing, so
        # it spends an attempt. The probe deliberately does not touch the article: it
        # is diagnostic only, and reviving old articles is out of scope.
        downloaded = bool(content and content.strip()) or error == _EMPTY_CONTENT_MSG

        if not downloaded:
            feed.readable_revival_attempts = (feed.readable_revival_attempts or 0) + 1
            _defer_revival(feed, now)
            await db.commit()
            logger.info(
                "readable revival: feed %d still blocked (attempt %d/%d, http %s, %s)",
                feed.id, feed.readable_revival_attempts, len(_REVIVAL_BACKOFF_DAYS),
                http_status, error,
            )
            continue

        subscribers = await _revive_readable_for_feed(feed, db)
        revived += 1
        logger.info(
            "readable revival: re-enabled extraction for feed %d (%d subscribers)",
            feed.id, subscribers,
        )

    return revived

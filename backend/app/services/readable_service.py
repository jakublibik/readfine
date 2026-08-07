"""Readable extraction pipeline: trafilatura → readability-lxml fallback."""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional
from urllib.parse import parse_qsl, urlsplit

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
from app.utils.http_client import READFINE_UA
from app.utils.parsing import count_words

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


# ── core extraction ───────────────────────────────────────────────────────────

def _fetch_html(
    url: str, auth_user: Optional[str], auth_pass: Optional[str]
) -> tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    """Download article HTML.

    Returns (html, error_message, http_status_code, final_url). *final_url* is where
    the redirect chain actually ended — the caller pasted address may be a click
    tracker or carry campaign parameters, and for a saved article that address is
    what ends up on screen and in the dedup key.
    """
    from app.utils.url_validator import log_outbound, validate_feed_url
    try:
        validate_feed_url(url)
    except ValueError as exc:
        logger.warning("readable URL blocked (SSRF): %s — %s", url, exc)
        return None, str(exc), None, None

    try:
        auth = (auth_user, auth_pass) if auth_user and auth_pass else None
        headers = {"User-Agent": READFINE_UA}
        current_url = url
        # http2=True: some CDNs 403 / hard-throttle HTTP/1.1 as a bot signal but serve
        # HTTP/2 normally (see url_validator._resolve_response).
        with httpx.Client(
            timeout=_TIMEOUT, follow_redirects=False, auth=auth, headers=headers, http2=True
        ) as client:
            for _ in range(_MAX_REDIRECTS + 1):
                started = time.monotonic()
                try:
                    resp = client.get(current_url)
                except Exception as exc:
                    log_outbound(current_url, None, started, error=type(exc).__name__)
                    raise
                # Extraction has its own client (no host throttling), so it must be
                # visible in the outbound log too — otherwise its share of a host's
                # rate-limit budget is invisible.
                log_outbound(current_url, resp, started)
                if not resp.is_redirect:
                    break
                redirect_url = resp.headers.get("location", "")
                # Resolve relative redirects against the current URL
                if redirect_url and not redirect_url.startswith(("http://", "https://")):
                    from urllib.parse import urljoin
                    redirect_url = urljoin(current_url, redirect_url)
                try:
                    validate_feed_url(redirect_url)
                except ValueError as exc:
                    logger.warning("readable redirect blocked (SSRF): %s — %s", redirect_url, exc)
                    return None, f"Redirect blocked: {exc}", None, None
                current_url = redirect_url
            else:
                return None, f"Too many redirects (max {_MAX_REDIRECTS})", None, None

        resp.raise_for_status()
        return resp.text, None, None, current_url
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


def _extract_with_trafilatura(html: str, url: str) -> Optional[str]:
    import re
    result = trafilatura.extract(html, url=url, output_format="html",
                                 include_comments=False, include_tables=True,
                                 include_links=True, include_images=True,
                                 favor_precision=True)
    if not result:
        return None
    # trafilatura outputs <graphic src="..." alt="..."/> instead of <img>
    result = re.sub(r'<graphic\b([^>]*)/>', r'<img\1>', result)
    return result


def _video_figure(provider: str, vid: str) -> str:
    """A video as stored in article content: thumbnail, link, and the ids to rebuild it.

    ``data-video-provider`` / ``data-video-id`` are what a player can be built from
    later without re-parsing the link. They survive sanitization (see ``_sanitize``),
    which means a feed can put them in its own markup too, so anything acting on them
    must validate the id rather than trust it — the same rule that applies to every
    other attribute arriving from a feed.
    """
    if provider == "youtube":
        href, thumb, caption = (
            f"https://www.youtube.com/watch?v={vid}",
            f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
            "Watch on YouTube",
        )
    else:
        href, thumb, caption = (
            f"https://vimeo.com/{vid}",
            f"https://vumbnail.com/{vid}.jpg",
            "Watch on Vimeo",
        )
    return (
        f'<figure data-video-provider="{provider}" data-video-id="{vid}">'
        f'<a href="{href}">'
        f'<img src="{thumb}" alt="Video thumbnail">'
        f'</a>'
        f'<figcaption>&#9654; {caption}</figcaption>'
        f'</figure>'
    )


def _collect_video_figures(html: str) -> list[str]:
    """
    Find YouTube/Vimeo iframes in raw HTML and return replacement <figure> strings.
    Trafilatura drops iframes, so we collect replacements before extraction
    and append them to the final content.
    """
    figures = []

    for m in re.finditer(r'<iframe\b[^>]*>.*?</iframe>', html, flags=re.DOTALL | re.IGNORECASE):
        iframe = m.group(0)
        src_m = re.search(r'\bsrc=["\']([^"\']+)["\']', iframe)
        if not src_m:
            continue
        src = src_m.group(1)

        yt = re.search(r'youtube(?:-nocookie)?\.com/embed/([A-Za-z0-9_-]+)', src)
        if yt:
            figures.append(_video_figure("youtube", yt.group(1)))
            continue

        vi = re.search(r'player\.vimeo\.com/video/(\d+)', src)
        if vi:
            figures.append(_video_figure("vimeo", vi.group(1)))

    return figures


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
        # Kept so a stored video survives as something a player can be built from
        # (see _video_figure). Both are inert ids, not URLs, and whatever reads them
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


def sends_us_back(
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


def _extract_og_description(html: str) -> Optional[str]:
    import html as html_mod

    head = _head_slice(html)
    m = _OG_DESC_RE.search(head) or _OG_DESC_ALT_RE.search(head)
    if not m:
        return None
    return re.sub(r"\s+", " ", html_mod.unescape(m.group(1))).strip()


# ── video pages ───────────────────────────────────────────────────────────────

# YouTube ids are 11 characters today, Vimeo's are digits. Both are bounded rather
# than pinned to a length, because the id ends up in a URL we build and the point of
# the pattern is that nothing else can get in there.
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_VIMEO_ID_RE = re.compile(r"^\d{5,15}$")
_YOUTUBE_HOSTS = {"youtube.com", "youtube-nocookie.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_ID_PATHS = ("/shorts/", "/embed/", "/live/", "/v/")


def _video_target(url: Optional[str]) -> Optional[tuple[str, str]]:
    """``(provider, video_id)`` when *url* is a page whose whole content is one video.

    Only pages that *are* a video qualify. A channel, a playlist or a search result is
    an ordinary page that happens to live on the same host, and running the video
    branch on one would replace a perfectly good listing with a single thumbnail.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().removeprefix("www.")
    path = parts.path

    if host in _YOUTUBE_HOSTS:
        vid = None
        if path == "/watch":
            vid = dict(parse_qsl(parts.query)).get("v")
        else:
            for prefix in _YOUTUBE_ID_PATHS:
                if path.startswith(prefix):
                    vid = path[len(prefix):].split("/", 1)[0]
                    break
        if vid and _YT_ID_RE.match(vid):
            return "youtube", vid
        return None

    if host == "youtu.be":
        vid = path.lstrip("/").split("/", 1)[0]
        return ("youtube", vid) if _YT_ID_RE.match(vid) else None

    if host == "vimeo.com":
        vid = path.lstrip("/").split("/", 1)[0]
        return ("vimeo", vid) if _VIMEO_ID_RE.match(vid) else None

    if host == "player.vimeo.com" and path.startswith("/video/"):
        vid = path[len("/video/"):].split("/", 1)[0]
        return ("vimeo", vid) if _VIMEO_ID_RE.match(vid) else None

    return None


# The description YouTube renders under the player, as it sits in the JSON the page
# ships for its own client. The alternation matches a JSON string body without
# backtracking: any character that is neither a quote nor a backslash, or an escape
# and whatever it escapes.
_YT_SHORT_DESC_RE = re.compile(r'"shortDescription":"((?:[^"\\]|\\.)*)"')


def _youtube_full_description(html: str) -> Optional[str]:
    """The video's whole description, or None when the page does not yield it.

    ``og:description`` is cut to about 160 characters with an ellipsis, which for a
    video is most of what there was to read. The full text is only in the payload the
    page hands its own JavaScript, so this reads it from there.

    That payload is YouTube's internal shape and carries no promise, so every failure
    path here is silent: the caller falls back to ``og:description`` and the article
    reads the way it would have without this.
    """
    m = _YT_SHORT_DESC_RE.search(html)
    if not m:
        return None
    try:
        text = json.loads(f'"{m.group(1)}"')
    except ValueError:
        return None
    text = text.strip()
    return text or None


# A chapter mark in a description: m:ss, mm:ss or h:mm:ss. The seconds field is what
# keeps this from firing on everything with a colon in it — a 16:9, a 3:2 or a 1:1 has
# no two-digit seconds to offer. What it does still catch is a clock time (10:30) and
# the occasional verse number, which become a link that seeks the video instead of
# meaning what they said. That is the accepted cost: in a video's own description a
# number of this shape is a point in the video far more often than it is anything else,
# and a seek is undone by seeking back.
_TIMESTAMP_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])")


def _timestamp_seconds(m: re.Match) -> int:
    return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _timestamp_href(provider: str, vid: str, seconds: int) -> str:
    """Where a timestamp points for anyone the player never reaches."""
    if provider == "youtube":
        return f"https://www.youtube.com/watch?v={vid}&t={seconds}s"
    return f"https://vimeo.com/{vid}#t={seconds}s"


def _link_timestamps(line: str, video: Optional[tuple[str, str]]) -> str:
    """Escape a line of description, turning chapter marks into seek links.

    ``data-seek`` is what the reader acts on: it seeks the player already on the page
    rather than opening the video somewhere else. The ``href`` is the same point on the
    site, so the mark still works where that script does not run.

    Nothing else in the text is linked. A description is mostly sponsor and affiliate
    URLs, and none of them is the article.
    """
    import html as html_mod

    if not video:
        return html_mod.escape(line)
    provider, vid = video
    out, pos = [], 0
    for m in _TIMESTAMP_RE.finditer(line):
        seconds = _timestamp_seconds(m)
        out.append(html_mod.escape(line[pos:m.start()]))
        out.append(
            f'<a href="{html_mod.escape(_timestamp_href(provider, vid, seconds))}" '
            f'data-seek="{seconds}">{html_mod.escape(m.group(0))}</a>'
        )
        pos = m.end()
    out.append(html_mod.escape(line[pos:]))
    return "".join(out)


def _description_paragraphs(text: Optional[str], video: Optional[tuple[str, str]] = None) -> str:
    """A video description as paragraphs, with everything in it treated as text.

    Pass *video* to make chapter marks clickable; without it the whole description is
    escaped and nothing in it links anywhere.
    """
    if not text:
        return ""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return "".join(
        "<p>" + "<br>".join(_link_timestamps(line, video) for line in block.split("\n")) + "</p>"
        for block in blocks
    )


def _video_page_content(provider: str, vid: str, html: str, og_description: Optional[str]) -> str:
    """The stored body for a video page: the video itself, then its description."""
    full = _youtube_full_description(html) if provider == "youtube" else None
    return (_video_figure(provider, vid)
            + _description_paragraphs(full or og_description, (provider, vid)))


def video_body_from_feed(
    url: Optional[str], description_text: Optional[str] = None, feed_html: Optional[str] = None
) -> Optional[str]:
    """The body for a feed item that is a video, or None when it is not one.

    A YouTube feed hands over everything this needs — the video id sits in the item's
    link and the whole description in the item itself — so the article can be built
    where it arrives, with no request to the watch page at all. That page is 1.4 MB,
    and fetching it would produce this same body from a description the feed had
    already given us.

    *description_text* is the item's own text, escaped and split into paragraphs the
    way a saved video's description is, timestamps and all. Pass it only when the text
    really is text: a feed that merely links to a video writes ordinary HTML in its
    items, and escaping that would put its markup on the screen. For those,
    *feed_html* is kept as it is and the video is placed above it.

    Note the caller must not run the result through a sanitizer: this is our own
    markup, and the ids the player is built from would not survive nh3's default
    attribute list.
    """
    video = _video_target(url)
    if not video:
        return None
    provider, vid = video
    if description_text is not None:
        return _video_figure(provider, vid) + _description_paragraphs(description_text, video)
    return _video_figure(provider, vid) + (feed_html or "")


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


def _extract_title(html: str) -> Optional[str]:
    """Page title from og:title, falling back to <title>.

    Deliberately regex over the head rather than a parse: the only caller is the
    save-by-URL path, and building a readability Document (or a second BeautifulSoup
    tree) just for a title would put a full lxml parse on every extraction if this
    ever moved onto the shared path.
    """
    import html as html_mod

    head = _head_slice(html)
    for pattern in (_OG_TITLE_RE, _TITLE_RE):
        m = pattern.search(head)
        if not m:
            continue
        title = html_mod.unescape(m.group(1))
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
    if is_4xx or error == _WRONG_CONTENT_MSG:
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

    if sends_us_back(final_url, url, html):
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

    video = _video_target(final_url) or _video_target(url)
    if video:
        # A watch page holds no prose to extract. Its description is drawn by the
        # page's own JavaScript, so the extractor finds nothing but the site footer
        # ("About, Press, Copyright, Contact us"), which is both worthless as an
        # article and — having nothing in common with the description — read as a
        # substitute page by _content_contradicts_page below, so saving a video link
        # failed with a consent-wall error on a page that had answered perfectly.
        # The video and its description are what the page is, so that is what is
        # stored, and neither the extractor nor that check gets a say.
        return ReadableResult(
            content=_video_page_content(*video, html, description),
            published_at=_find_published_date(html, url), title=title,
            resolved_url=resolved_url, description=description,
        )

    video_figures = _collect_video_figures(html)
    html = _strip_pre_extraction_noise(html)
    content = _extract_with_trafilatura(html, url)
    if not content:
        content = _extract_with_readability(html)
    if not content:
        logger.warning("readable extraction yielded no content for %s", url)
        return ReadableResult(error=_EMPTY_CONTENT_MSG, title=title,
                              resolved_url=resolved_url, description=description)

    if video_figures:
        content += "\n" + "\n".join(video_figures)
    from app.utils.parsing import rewrite_relative_urls, soften_nbsp_runs
    final = rewrite_relative_urls(
        soften_nbsp_runs(_drop_empty_blocks(_dedupe_images(_sanitize(content)))), url)
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
    feed_auth: dict[int, tuple[Optional[str], Optional[str]]] = {}
    for feed_id, auth_user, auth_pass_enc in feeds_result:
        decrypted_pass: Optional[str] = None
        if auth_pass_enc:
            try:
                from app.utils.crypto import decrypt
                decrypted_pass = decrypt(auth_pass_enc)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt fetch_auth_pass for feed %d: %s", feed_id, exc
                )
        feed_auth[feed_id] = (auth_user, decrypted_pass)

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

        # Articles saved by URL have no feed. They must not touch the per-feed
        # bookkeeping below — every one of them would land in the same `None` bucket,
        # so unrelated hosts would pool their 403s/empties and could trip
        # _disable_readable_for_403(None, db) for a feed that does not exist.
        is_feedless = article.feed_id is None

        auth_user, auth_pass = feed_auth.get(article.feed_id, (None, None))
        title = None
        resolved_url = None
        description = None
        try:
            if is_feedless:
                r = await loop.run_in_executor(
                    None, extract_readable_with_title,
                    article.url, auth_user, auth_pass, True
                )
                content, error, http_status, published_at = (
                    r.content, r.error, r.http_status, r.published_at
                )
                title, resolved_url, description = r.title, r.resolved_url, r.description
            else:
                content, error, http_status, published_at = await loop.run_in_executor(
                    None, extract_readable, article.url, auth_user, auth_pass
                )
        except Exception as exc:
            content, error, http_status, published_at = None, str(exc)[:200], None, None
            logger.warning("readable extraction error for article %d: %s", article.id, exc)

        # Re-check status — on-demand extraction may have already processed this article
        await db.refresh(article)
        if article.readable_status == "success":
            processed += 1
            continue

        is_403 = apply_readable_result(
            article, content, error, http_status, published_at,
            title=title, description=description,
        )
        if is_feedless:
            from app.services.saved_article_service import _adopt_resolved_url
            _adopt_resolved_url(article, resolved_url)
        is_empty = content is None and error == _EMPTY_CONTENT_MSG
        from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
        if is_feedless:
            # Saved-by-URL: no scoring, and post-processing is per-saver. This is the
            # fallback path for an import task that died or hit a transient error —
            # without it those articles would come out fully extracted but silently
            # never filtered.
            if content or article.readable_status == "failed":
                from app.services.saved_article_service import finalize_for_all_savers
                await finalize_for_all_savers(article, db)
            processed += 1
            await db.commit()
            continue

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

async def maybe_disable_readable_for_feed(feed_id: int, db: AsyncSession) -> bool:
    """
    Check if a feed consistently delivers full content by itself.
    If so, disable extract_readable on all UserFeed rows for this feed.
    Returns True if disabled.

    The counts are recomputed from Article.content, the body as it arrived in the feed,
    and deliberately not read from Article.word_count: a successful extraction
    overwrites that column with the word count of the *extracted page* (see
    apply_readable_result), so a feed whose extraction works well would read as a
    full-content feed and turn its own extraction off. Trimmed articles are left out
    because retention has already dropped or replaced their body.

    Runs on every fetch that brings new articles, so it asks who would be affected
    before it measures anything. On a feed nobody extracts, which includes every feed
    this check has already disabled, the answer changes nothing, and those are exactly
    the feeds with the largest bodies to read.
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

    result = await db.execute(
        # Only the head of each body travels: the question is whether it clears 500
        # words, and _FULL_CONTENT_SAMPLE_CHARS is far more room than that needs, so
        # the sample costs the same on a feed of 500-word posts as on one of essays.
        select(func.substr(Article.content, 1, _FULL_CONTENT_SAMPLE_CHARS))
        .where(
            Article.feed_id == feed_id,
            Article.content.isnot(None),
            Article.content != "",  # sanitizer emptied it: no body to measure
            Article.trimmed_at.is_(None),
        )
        .order_by(Article.id.desc())
        .limit(_FULL_CONTENT_SAMPLE)
    )
    counts = [count_words(row[0]) for row in result]
    if len(counts) < _FULL_CONTENT_SAMPLE:
        return False  # not enough data yet

    full_content = sum(1 for c in counts if c > _FULL_CONTENT_MIN_WORDS)
    if full_content / len(counts) < _FULL_CONTENT_THRESHOLD:
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
        feed_id, full_content, len(counts),
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


def _defer_revival(feed: Feed, now: datetime) -> None:
    """Point a feed at its next revival probe, or stop probing for good.

    Reads Feed.readable_revival_attempts, which counts every probe the feed has been
    given over its whole lifetime, passing ones included, and is never reset by this
    module. Both halves of that matter. A 403 is usually per-IP or rate-based rather
    than per-URL, so a probe can pass while the feed is still blocked: extraction comes
    back on, users collect 403s, one article later the feed is disabled again (the old
    403 articles are still the newest terminal ones, so the streak check trips at once).
    If a passing probe were free, or the counter reset on re-disable, that would repeat
    every few days forever. Charging for it lets the loop end by itself.
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


async def _maybe_disable_readable_for_403(feed_id: int, db: AsyncSession) -> None:
    """Disable readable if the last N processed articles for the feed all returned 403.

    Used for cross-batch detection: when a feed accumulates 403s across multiple scheduler
    runs (few articles per batch), this catches it once the consecutive count is reached.
    """
    result = await db.execute(
        select(Article.readable_status, Article.readable_error)
        .where(
            Article.feed_id == feed_id,
            Article.readable_status.in_(["failed", "success"]),
        )
        .order_by(Article.id.desc())
        .limit(_CONSECUTIVE_403_THRESHOLD)
    )
    rows = result.all()

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
    result = await db.execute(
        select(Article.readable_status, Article.readable_error)
        .where(
            Article.feed_id == feed_id,
            Article.readable_status.in_(["failed", "success"]),
        )
        .order_by(Article.id.desc())
        .limit(_CONSECUTIVE_EMPTY_THRESHOLD)
    )
    rows = result.all()

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

        auth_pass: Optional[str] = None
        if feed.fetch_auth_pass_encrypted:
            try:
                from app.utils.crypto import decrypt
                auth_pass = decrypt(feed.fetch_auth_pass_encrypted)
            except Exception as exc:
                logger.warning(
                    "Failed to decrypt fetch_auth_pass for feed %d: %s", feed.id, exc
                )

        try:
            content, error, http_status, _ = await loop.run_in_executor(
                None, extract_readable, article_url, feed.fetch_auth_user, auth_pass
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

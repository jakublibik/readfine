"""Recognising videos and turning them into the markup an article stores.

Lives in utils rather than beside readable extraction because both sides of the
application need it and neither should have to reach across for it: the RSS
fetcher builds a video article straight from the feed item, and readable
extraction builds one from a watch page it downloaded. Nothing here touches the
database, the network or a request, so it stays a set of pure functions.

What gets stored is a facade, never a player: a thumbnail, a link to the video's
own site, and the two ids the front end rebuilds an embed from once the reader
presses play (see ``app.js``). Until then no request reaches YouTube or Vimeo.
"""
import html as html_mod
import json
import re
from urllib.parse import parse_qsl, urlsplit

# YouTube ids are 11 characters today, Vimeo's are digits. Both are bounded rather
# than pinned to a length, because the id ends up in a URL we build and the point of
# the pattern is that nothing else can get in there.
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_VIMEO_ID_RE = re.compile(r"^\d{5,15}$")
_YOUTUBE_HOSTS = {"youtube.com", "youtube-nocookie.com", "m.youtube.com", "music.youtube.com"}
_YOUTUBE_ID_PATHS = ("/shorts/", "/embed/", "/live/", "/v/")


def video_figure(provider: str, vid: str) -> str:
    """A video as stored in article content: thumbnail, link, and the ids to rebuild it.

    ``data-video-provider`` / ``data-video-id`` are what a player can be built from
    later without re-parsing the link. They survive sanitization (see
    ``readable_service._sanitize``), which means a feed can put them in its own markup
    too, so anything acting on them must validate the id rather than trust it — the
    same rule that applies to every other attribute arriving from a feed.
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


def collect_video_figures(html: str) -> list[str]:
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
            figures.append(video_figure("youtube", yt.group(1)))
            continue

        vi = re.search(r'player\.vimeo\.com/video/(\d+)', src)
        if vi:
            figures.append(video_figure("vimeo", vi.group(1)))

    return figures


def video_target(url: str | None) -> tuple[str, str] | None:
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


def youtube_full_description(html: str) -> str | None:
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


def _link_timestamps(line: str, video: tuple[str, str] | None) -> str:
    """Escape a line of description, turning chapter marks into seek links.

    ``data-seek`` is what the reader acts on: it seeks the player already on the page
    rather than opening the video somewhere else. The ``href`` is the same point on the
    site, so the mark still works where that script does not run.

    Nothing else in the text is linked. A description is mostly sponsor and affiliate
    URLs, and none of them is the article.
    """
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


def description_paragraphs(text: str | None, video: tuple[str, str] | None = None) -> str:
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


def video_page_content(
    provider: str, vid: str, html: str, og_description: str | None
) -> str:
    """The stored body for a video page: the video itself, then its description."""
    full = youtube_full_description(html) if provider == "youtube" else None
    return (video_figure(provider, vid)
            + description_paragraphs(full or og_description, (provider, vid)))


def video_body_from_feed(
    url: str | None, description_text: str | None = None, feed_html: str | None = None
) -> str | None:
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
    video = video_target(url)
    if not video:
        return None
    provider, vid = video
    if description_text is not None:
        return video_figure(provider, vid) + description_paragraphs(description_text, video)
    return video_figure(provider, vid) + (feed_html or "")

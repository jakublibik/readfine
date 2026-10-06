"""MediaWiki pages for readable extraction: infoboxes and data tables.

A MediaWiki page (Wikipedia and its kin) keeps its infobox and data tables in
markup the extractors drop or flatten. They are lifted out before extraction,
rendered on their own, and put back into the extracted body afterwards.
"""
import html as html_mod
import re
from typing import NamedTuple

from bs4 import BeautifulSoup

from app.services.readable_checks import _head_slice
from app.utils.parsing import count_text_words


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
        words = count_text_words(box.get_text(" ", strip=True))
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
    # Imported here: readable_service imports this module, and the sanitizer is the
    # extraction's own, not something the wiki handling should keep a copy of.
    from app.services.readable_service import _sanitize
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

#!/usr/bin/env python
"""Run readable extraction over a spread of live pages and flag suspect results.

Readable extraction is judged by how a page reads, which no unit test can assert, so
this is the tool that looks. It fetches a corpus of real URLs through the same code
path the app uses -- SSRF validation, size caps and all -- and scores each result for
the failure shapes that have actually turned up: a table that came apart into loose
paragraphs, an article that lost its headings, a page that extracted to navigation, a
leftover placeholder that should have been substituted away.

It is deliberately NOT a pytest test. It needs the network, third-party sites change
under it, and a handful of them answer a datacenter IP with 403 no matter what, so a
red run here is a prompt to go and look rather than a build failure. Run it after
touching readable_service, compare against the last run, and turn anything it finds
into a real unit test with a saved fixture.

Usage, from the repository root::

    uv run --project backend python scripts/survey_extraction.py
    uv run --project backend python scripts/survey_extraction.py --only wiki,docs
    uv run --project backend python scripts/survey_extraction.py --urls my_list.txt
    uv run --project backend python scripts/survey_extraction.py --json out.json

A URL file is one ``category<TAB>url`` (or ``category url``) per line, ``#`` for
comments. The built-in corpus is not a list of good pages: it is a list of *different*
pages, chosen so that saving by URL is represented by more than news sites -- docs,
wikis, code hosting, Q&A, forums, blogs, papers, recipes, legislation, shops, comics,
e-books. Several entries are known-hard on purpose (Reddit extracts to nothing,
Hacker News is built from layout tables); they are here to stay visible, not because
they are expected to pass.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from bs4 import BeautifulSoup  # noqa: E402

from app.services import readable_service as rs  # noqa: E402

# One page per shape of page, not per site. Keep it varied rather than long: the point
# is coverage of *kinds* of markup, and every entry costs a live request.
CORPUS: list[tuple[str, str]] = [
    ("docs", "https://developer.mozilla.org/en-US/docs/Web/CSS/position"),
    ("docs", "https://docs.python.org/3/library/asyncio-task.html"),
    ("docs", "https://kubernetes.io/docs/concepts/workloads/pods/"),
    ("docs", "https://doc.rust-lang.org/book/ch04-01-what-is-ownership.html"),
    ("docs", "https://man7.org/linux/man-pages/man1/grep.1.html"),
    ("docs", "https://www.postgresql.org/docs/current/sql-select.html"),
    ("wiki", "https://en.wikipedia.org/wiki/Ucchusma"),
    ("wiki", "https://en.wikipedia.org/wiki/Prague"),
    ("wiki", "https://wiki.archlinux.org/title/Systemd"),
    ("wiki", "https://en.wikivoyage.org/wiki/Prague"),
    ("wiki", "https://cs.wikipedia.org/wiki/Brno"),
    ("code", "https://github.com/astral-sh/uv"),
    ("code", "https://github.com/tailwindlabs/tailwindcss/releases/tag/v4.0.0"),
    ("qa", "https://stackoverflow.com/questions/11227809/why-is-processing-a-sorted-array-faster-than-processing-an-unsorted-array"),
    ("forum", "https://news.ycombinator.com/item?id=42297424"),
    ("forum", "https://www.reddit.com/r/selfhosted/comments/1h0z1qk/"),
    ("forum", "https://discuss.python.org/t/pep-750-tag-strings/60408"),
    ("blog", "https://simonwillison.net/2024/Dec/31/llms-in-2024/"),
    ("blog", "https://blog.rust-lang.org/2024/11/28/Rust-1.83.0.html"),
    ("blog", "https://danluu.com/percentile-latency/"),
    ("blog", "https://overreacted.io/a-chain-reaction/"),
    ("blog", "https://jvns.ca/blog/2024/11/18/how-to-import-a-javascript-library/"),
    ("newsletter", "https://newsletter.pragmaticengineer.com/p/the-pulse-118"),
    ("academic", "https://arxiv.org/abs/1706.03762"),
    ("academic", "https://www.nature.com/articles/s41586-023-06924-6"),
    ("academic", "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0301757"),
    ("academic", "https://pubmed.ncbi.nlm.nih.gov/33301246/"),
    ("recipe", "https://www.bbcgoodfood.com/recipes/classic-lasagne"),
    ("gov", "https://www.zakonyprolidi.cz/cs/2012-89"),
    ("product", "https://www.apple.com/macbook-air/"),
    ("product", "https://tailscale.com/kb/1017/install"),
    ("longform", "https://www.theatlantic.com/technology/archive/2024/12/ai-search-engines/680975/"),
    ("video", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("misc", "https://xkcd.com/2347/"),
    ("misc", "https://www.gutenberg.org/files/1342/1342-h/1342-h.htm"),
    ("misc", "https://ourworldindata.org/grapher/life-expectancy"),
    ("misc", "https://caniuse.com/css-container-queries"),
]

# Markup that should never reach a reader. Each one is an extractor vocabulary or an
# internal placeholder that a conversion step was supposed to have dealt with.
ARTEFACTS = ("RFDATATABLE", "<row", "<cell", "<graphic")

_WS = re.compile(r"\s+")


def _text(html: str | None) -> str:
    return _WS.sub(" ", BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True))


def diagnose(category: str, url: str) -> dict:
    """Fetch, extract, and score one page. Never raises: a crash is a result too."""
    row: dict = {"cat": category, "url": url}
    try:
        raw, fetch_error, status, _final = rs._fetch_html(url, None, None)
    except Exception as exc:  # noqa: BLE001 - one bad page must not end the run
        return row | {"verdict": "fetch-crash", "note": str(exc)[:120]}
    if not raw:
        return row | {"verdict": "fetch-failed", "note": (fetch_error or "")[:120],
                      "http": status}

    # The page's own text, as the ceiling on what extraction could have returned.
    source = BeautifulSoup(raw, "html.parser")
    for tag in source(["script", "style", "noscript"]):
        tag.decompose()
    src_words = len(_text(str(source)).split())
    src_tables = len(source.find_all("table"))
    src_heads = len(source.find_all(["h1", "h2", "h3", "h4"]))

    result = rs.extract_readable_with_title(url, reject_wrong_content=True)
    row |= {"src_words": src_words, "src_tables": src_tables, "src_heads": src_heads,
            "title": (result.title or "")[:70]}
    if not result.content:
        return row | {"verdict": "no-content", "note": (result.error or "")[:120]}

    out = result.content
    soup = BeautifulSoup(out, "html.parser")
    text = _text(out)
    words = len(text.split())
    paragraphs = soup.find_all("p")
    tiny = sum(1 for p in paragraphs if 0 < len(p.get_text(strip=True)) <= 20)
    link_chars = sum(len(a.get_text(" ", strip=True)) for a in soup.find_all("a"))

    row |= {
        "words": words,
        "heads": len(soup.find_all(["h1", "h2", "h3", "h4"])),
        "tr": len(soup.find_all("tr")),
        "imgs": len(soup.find_all("img")),
        "paras": len(paragraphs),
        "tiny_paras": tiny,
        "link_density": round(link_chars / max(len(text), 1), 3),
        "coverage": round(words / max(src_words, 1), 3),
    }

    flags = []
    flags += [f"artefact:{a.lstrip('<')}" for a in ARTEFACTS if a in out]
    if re.search(r"&(?:#\d+|[a-z]+);", text):
        flags.append("undecoded-entity")
    if src_tables and row["tr"] == 0:
        # The shape that made a Wikipedia climate table read as a column of numbers.
        flags.append("table-lost")
    if tiny >= 8 and tiny > len(paragraphs) * 0.3:
        flags.append("fragmented")
    if src_heads >= 4 and row["heads"] == 0:
        flags.append("headings-lost")
    if words < 60:
        flags.append("very-short")
    if row["coverage"] < 0.05 and src_words > 400:
        flags.append("low-coverage")
    if row["link_density"] > 0.5 and words > 80:
        flags.append("link-farm")

    return row | {"flags": flags, "verdict": "flagged" if flags else "ok"}


def load_urls(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split(None, 1)
        pairs.append((parts[0], parts[1]) if len(parts) == 2 else ("misc", parts[0]))
    return pairs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--urls", type=Path, help="file of 'category<TAB>url' lines")
    parser.add_argument("--only", help="comma-separated categories to run")
    parser.add_argument("--json", type=Path, help="write the full rows here")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    corpus = load_urls(args.urls) if args.urls else CORPUS
    if args.only:
        wanted = {c.strip() for c in args.only.split(",")}
        corpus = [(c, u) for c, u in corpus if c in wanted]
    if not corpus:
        print("nothing to do", file=sys.stderr)
        return 1

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda pair: diagnose(*pair), corpus))

    if args.json:
        args.json.write_text(json.dumps(rows, indent=1, ensure_ascii=False), encoding="utf-8")

    counts = collections.Counter(row["verdict"] for row in rows)
    print(f"{len(rows)} pages: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))

    problems = [r for r in rows if r["verdict"] != "ok"]
    if problems:
        print("\n── needs a look " + "─" * 60)
    for row in problems:
        print(f"[{row['verdict']:12s}] {row['cat']:11s} {row['url'][:70]}")
        if row.get("flags"):
            print(f"{'':14s} {row['flags']}  words={row.get('words')} "
                  f"cov={row.get('coverage')} tr={row.get('tr')}/{row.get('src_tables')} "
                  f"h={row.get('heads')}/{row.get('src_heads')} "
                  f"tiny={row.get('tiny_paras')}/{row.get('paras')}")
        if row.get("note"):
            print(f"{'':14s} {row['note']}")

    print("\n── clean " + "─" * 67)
    for row in rows:
        if row["verdict"] == "ok":
            print(f"  {row['cat']:11s} words={row['words']:7d} cov={row['coverage']:.2f} "
                  f"h={row['heads']:4d} tr={row['tr']:4d} link={row['link_density']:.2f}  "
                  f"{row['url'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

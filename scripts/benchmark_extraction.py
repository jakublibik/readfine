#!/usr/bin/env python
"""Score readable extraction against a corpus with a human-written ground truth.

``survey_extraction.py`` is the other half of this pair and answers a different
question. It fetches live pages and flags shapes that look wrong, which is how an
unknown failure gets noticed, but every one of its signals is a proxy -- word counts,
heading counts, link density -- and a proxy can be read the wrong way round. This
script removes the guessing: it runs the extractors over saved pages whose article
text a person has written out by hand, and scores the result against it.

The corpus is Zyte's ``article-extraction-benchmark`` (181 pages, MIT licensed),
downloaded on first run to ``.benchmark/`` in the repository root, which is
gitignored. Scoring uses that project's own token-level F1 so the numbers here can be
compared with the ones it publishes.

**What it does and does not cover.** The corpus is news and blog articles, because
that is what it was built for. It is the right instrument for "would this change hurt
ordinary articles" and says nothing at all about documentation, wikis, forums or
reference pages. A change that scores level here can still be wrong on a man page.
Use both scripts: this one to show a change does no harm, the survey to show it does
some good.

Usage, from the repository root::

    uv run --project backend python scripts/benchmark_extraction.py
    uv run --project backend python scripts/benchmark_extraction.py --limit 40
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import sys
import tarfile
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

BENCH_DIR = REPO_ROOT / ".benchmark" / "article-extraction-benchmark-master"
ARCHIVE_URL = (
    "https://codeload.github.com/scrapinghub/article-extraction-benchmark/"
    "tar.gz/refs/heads/master"
)

from bs4 import BeautifulSoup  # noqa: E402

from app.services import readable_service as rs  # noqa: E402


def ensure_corpus() -> Path:
    """Download and unpack the benchmark once. ~29 MB."""
    if (BENCH_DIR / "ground-truth.json").exists():
        return BENCH_DIR
    BENCH_DIR.parent.mkdir(parents=True, exist_ok=True)
    print(f"downloading benchmark corpus to {BENCH_DIR.parent} ...", file=sys.stderr)
    with urllib.request.urlopen(ARCHIVE_URL, timeout=300) as response:
        payload = response.read()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        archive.extractall(BENCH_DIR.parent, filter="data")
    return BENCH_DIR


def to_text(html: str | None) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def headings(html: str | None) -> int:
    if not html:
        return 0
    return len(BeautifulSoup(html, "html.parser").find_all(["h1", "h2", "h3", "h4"]))


_GT: dict = {}


def _extract(key: str) -> tuple[str, str, str, str, bool]:
    url = _GT[key]["url"]
    path = BENCH_DIR / "html" / f"{key}.html.gz"
    html = gzip.open(path, "rt", encoding="utf8", errors="replace").read()
    traf = rs._extract_with_trafilatura(html, url) or ""
    read = rs._extract_with_readability(html) or ""
    gated = bool(traf) and rs._prefer_readability(traf, html)
    # Only where the app would ask for them, so the cost here matches the cost there.
    # Both retries collapse into one column: they are alternatives, and the first with
    # enough headings is the one the app would keep.
    retry = ""
    if gated:
        repaired = rs._repair_headings(html)
        for candidate in (rs._extract_with_trafilatura(repaired, url),
                          rs._extract_with_trafilatura(repaired, url, favor_precision=False)):
            if headings(candidate) >= rs._MIN_FALLBACK_HEADINGS:
                retry = candidate or ""
                break
    return key, traf, read, retry, gated


def _init(ground_truth: dict) -> None:
    global _GT
    _GT = ground_truth


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="score only the first N pages")
    parser.add_argument("--workers", type=int, default=os.cpu_count())
    args = parser.parse_args()

    bench = ensure_corpus()
    sys.path.insert(0, str(bench))
    from evaluate import metrics_from_tp_fp_fns, string_shingle_matching  # noqa: E402

    # encoding is explicit because the default is the locale's, which on a Windows
    # console is cp1250 and cannot read this file at all.
    ground_truth = json.loads((bench / "ground-truth.json").read_text(encoding="utf8"))
    keys = list(ground_truth)[: args.limit] if args.limit else list(ground_truth)

    with ProcessPoolExecutor(max_workers=args.workers,
                             initializer=_init, initargs=(ground_truth,)) as pool:
        rows = list(pool.map(_extract, keys))
    traf = {k: t for k, t, _, _, _ in rows}
    read = {k: r for k, _, r, _, _ in rows}
    retry = {k: q for k, _, _, q, _ in rows}
    gated = {k: g for k, _, _, _, g in rows}

    def score(name: str, chosen: dict[str, str]) -> None:
        pairs = [
            string_shingle_matching(true=ground_truth[k]["articleBody"], pred=to_text(v))
            for k, v in chosen.items()
        ]
        m = metrics_from_tp_fp_fns(pairs)
        print(f"  {name:<38} F1={m['f1']:.3f}  precision={m['precision']:.3f}  "
              f"recall={m['recall']:.3f}")

    print(f"\n{len(keys)} pages, Zyte article-extraction-benchmark, token-level F1\n")
    score("trafilatura only", traf)
    score("readability only", read)
    # What the app does today: readability is a fallback for an empty result only.
    score("pipeline as shipped",
          {k: (traf[k] if traf[k].strip() else read[k]) for k in keys})
    # The structure gate: trafilatura returned prose with no headings at all while
    # readability found several, which on reference pages means trafilatura flattened
    # the document. On news it never triggers, which is the point of measuring here.
    fires = [k for k in keys if headings(traf[k]) == 0 and headings(read[k]) >= 4]
    score("headings gate",
          {k: (read[k] if k in fires else (traf[k] or read[k])) for k in keys})
    # Behind the gate, trafilatura gets two more goes at the page (permalinks stripped,
    # then without its precision bias) and readability is only reached if both still
    # find no headings. GitHub and jvns.ca are the pages these were written for and
    # neither is in this corpus, so what the row is here to show is that the extra
    # steps cost the corpus nothing.
    repaired = [k for k in keys if retry[k] and headings(retry[k]) >= 4]

    def resolved(k: str) -> str:
        if retry[k] and headings(retry[k]) >= 4:
            return retry[k]
        if k in fires:
            return read[k]
        return traf[k] or read[k]

    score("headings gate + permalink repair", {k: resolved(k) for k in keys})

    # Two different numbers, because reading one for the other is easy and wrong. The
    # gate's own condition (trafilatura kept no heading on a page that has several) is
    # common; what is rare is a second reading good enough to be taken instead, which
    # is the only thing that can move the score.
    shipped = {k: (traf[k] or read[k]) for k in keys}
    changed = [k for k in keys if resolved(k) is not shipped[k] and resolved(k) != shipped[k]]
    print(f"\n  the gate's condition holds on {sum(gated.values())}/{len(keys)} pages")
    print(f"  readability is preferred on   {len(fires)}/{len(keys)}")
    print(f"  a trafilatura retry wins on   {len(repaired)}/{len(keys)}")
    print(f"  the page that is stored differs from today on {len(changed)}/{len(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

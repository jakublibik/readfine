# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "sentence-transformers>=3.0",
#   "scikit-learn>=1.5",
#   "numpy>=1.26",
#   "py3langid>=0.3",
#   "sqlalchemy>=2.0",
# ]
# ///
"""Phase 1 of the offline embedding-vs-LLM scoring test.

Scores every exported article with a local embedding model (cosine against the
interest profile) and with a TF-IDF baseline, then compares both to the stored
LLM score on the same articles, against the same engagement label the admin eval
uses.

    uv run --script run_eval.py --sample sample.jsonl --out results.json

The sample is split into profile regimes: each segment is scored against the
profile text that was actually live in it, so the embedding sees what the LLM
saw. AUC is computed inside a segment and only the difference is pooled, because
the segments have different base rates.

`compute_auc` is imported from the app rather than reimplemented: it handles
ties, which is exactly where a hand-rolled AUC goes wrong on quantized LLM
scores.
"""
import argparse
import csv
import hashlib
import json
import random
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "backend"))
from app.services.ai_eval_service import (  # noqa: E402
    calibration_buckets,
    compute_auc,
    score_histogram,
)

CONTENT_MAX_CHARS = 2000  # ai_scoring_service._CONTENT_MAX_CHARS

# e5 models are trained with these prefixes; dropping them costs real quality and
# then looks like a property of the model. Other families get their own or none.
PREFIXES = {
    "e5": ("query: ", "passage: "),
    "bge-m3": ("", ""),
    "none": ("", ""),
}

# Splitting on the Czech conjunction "a" is deliberately left out: it collides
# with the English article, and the profile is written in English by default.
_TOPIC_SEPARATOR_RE = re.compile(r"[,;]|\band\b|\bnebo\b")
# The generator writes the profile as `label: topics` lines and asks for
# "High relevance / Moderate relevance / Avoid" (`ai_profile_service.
# normalize_preference_text`), but the model may translate or reword the labels,
# so negativity is matched loosely and anything unrecognised counts as positive.
_NEGATIVE_LABEL_RE = re.compile(
    r"avoid|exclude|not interested|no interest|dislike|skip|irrelevant|"
    r"nezajím|vyhýb|vynech|nechci", re.IGNORECASE)


# ── sample loading ────────────────────────────────────────────────────────────

def load_sample(path: Path) -> tuple[dict, list[dict]]:
    meta, rows = None, []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("type") == "meta":
                meta = obj
            else:
                rows.append(obj)
    if meta is None:
        raise SystemExit("sample has no meta line")
    return meta, rows


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def scoring_time(row: dict) -> datetime:
    """When the score was written, not when the article arrived.

    Applying a filter retroactively scores older articles, so those two differ;
    splitting by arrival would then compare a score against a profile that did
    not exist yet. Falls back to the state timestamp for rows whose scoring job
    is gone.
    """
    return parse_dt(row.get("scored_at") or row["state_created_at"])


def assign_segments(meta: dict, rows: list[dict]) -> dict:
    """Split the sample by profile regime, newest first.

    The last two regenerations bound the two clean segments: P3 runs from the
    most recent regeneration (current profile text), P2 from the one before it
    (the text kept in `ai_preference_prev_text`). Anything older has no profile
    text left, so it only feeds the control run against today's profile.
    """
    regens = sorted((parse_dt(r["at"]) for r in meta["profile_regenerations"]),
                    reverse=True)
    if len(regens) < 2:
        raise SystemExit("need at least two profile regenerations to split segments")
    p3_start, p2_start = regens[0], regens[1]

    segments = {
        "P2": {"start": p2_start, "end": p3_start,
               "profile": meta["profile"]["previous"], "rows": []},
        "P3": {"start": p3_start, "end": None,
               "profile": meta["profile"]["current"], "rows": []},
    }
    older = []
    for row in rows:
        scored = scoring_time(row)
        if scored >= p3_start:
            segments["P3"]["rows"].append(row)
        elif scored >= p2_start:
            segments["P2"]["rows"].append(row)
        else:
            older.append(row)
    segments["_older"] = older
    for name in ("P2", "P3"):
        if segments[name]["profile"] is None:
            raise SystemExit(f"segment {name} has no profile text in the sample meta")
    return segments


def drop_post_retention(meta: dict, rows: list[dict],
                        margin_days: int) -> tuple[list[dict], dict]:
    """Cut articles old enough for purge to have thinned the sample.

    Past T1 (`default_purge_after_days`) purge keeps starred and archived
    articles in full, trims merely-engaged ones to a stub and deletes everything
    else. An untrimmed article older than that is therefore a survivor: the
    engagement rate there approaches 100% no matter how good the scoring was.
    Keeping those rows would inflate every AUC in the run.
    """
    t1 = meta.get("purge_after_days")
    if not t1:
        return rows, {"applied": False, "reason": "no purge_after_days in meta"}
    cutoff_days = t1 - margin_days
    if cutoff_days <= 0:
        raise SystemExit(f"retention margin {margin_days} is not smaller than "
                         f"T1 ({t1} days); nothing would be left")
    exported = parse_dt(meta["exported_at"])

    kept, dropped = [], []
    for row in rows:
        fetched = row.get("fetched_at")
        age = (exported - parse_dt(fetched)).days if fetched else 0
        (dropped if age >= cutoff_days else kept).append(row)
    stats = {
        "applied": True,
        "t1_days": t1,
        "margin_days": margin_days,
        "cutoff_days": cutoff_days,
        "kept": len(kept),
        "dropped": len(dropped),
        "dropped_engaged_rate": (sum(1 for r in dropped if r["engaged"]) / len(dropped)
                                 if dropped else None),
        "kept_engaged_rate": (sum(1 for r in kept if r["engaged"]) / len(kept)
                              if kept else None),
    }
    return kept, stats


def sample_warnings(meta: dict, rows: list[dict]) -> list[str]:
    """Assumptions the run depends on, checked against the data instead of memory."""
    warnings = []
    regens = sorted((parse_dt(r["at"]) for r in meta["profile_regenerations"]),
                    reverse=True)
    updated = meta["profile"].get("updated_at")
    if updated and regens and abs((parse_dt(updated) - regens[0]).total_seconds()) > 3600:
        warnings.append(
            f"profile.updated_at ({updated}) does not match the last regeneration "
            f"({regens[0].isoformat()}): the current text is not what P3 was scored "
            f"against (edited by hand?)")
    if meta["profile"].get("auto_days"):
        warnings.append(
            f"auto_days={meta['profile']['auto_days']} is still on, so the profile "
            f"can regenerate mid-test and shrink the clean window")

    missing_job = sum(1 for r in rows if not r.get("scored_at"))
    if missing_job:
        warnings.append(f"{missing_job} rows have no scoring job; segmented by "
                        f"state created_at instead")
    retro = sum(1 for r in rows if r.get("scored_at")
                and (parse_dt(r["scored_at"]) - parse_dt(r["state_created_at"])).days >= 1)
    if retro:
        warnings.append(f"{retro} rows were scored a day or more after arrival "
                        f"(retroactive filter runs)")
    empty = sum(1 for r in rows if not (r.get("body") or "").strip())
    if empty:
        warnings.append(f"{empty} rows have an empty body; scored on the title alone")

    models = {r.get("job_model") for r in rows if r.get("job_model")}
    if len(models) > 1:
        warnings.append(f"scores come from more than one model: {sorted(models)}")
    return warnings


# ── text preparation ──────────────────────────────────────────────────────────

def article_text(row: dict, variant: str) -> str:
    title, body = row["title"] or "", row["body"] or ""
    if variant == "title":
        return title
    if variant == "title300":
        return f"{title}\n\n{body[:300]}".strip()
    if variant == "full2000":
        combined = f"{title}\n\n{body}" if body else title
        return combined[:CONTENT_MAX_CHARS]
    raise SystemExit(f"unknown input variant {variant}")


def split_topics(line: str) -> list[str]:
    """Split a topic list on separators that sit outside brackets.

    The generator writes topics like "health science with mechanistic findings
    (nutrition, exercise, longevity)". Splitting on every comma turns that into
    bare "exercise" and "longevity)" — fragments that have lost the very context
    that made them a topic, and that then match unrelated articles.
    """
    parts, depth, current = [], 0, []
    tokens = _TOPIC_SEPARATOR_RE.split(line)
    separators = _TOPIC_SEPARATOR_RE.findall(line)
    for i, token in enumerate(tokens):
        current.append(token)
        depth += token.count("(") - token.count(")")
        if i < len(separators):
            if depth > 0:  # separator inside brackets: keep the topic together
                current.append(separators[i])
            else:
                parts.append("".join(current))
                current = []
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


def parse_profile(profile: str, mode: str, max_lines: int | None,
                  max_topics: int | None = None) -> tuple[list[str], list[str]]:
    """Split the profile into positive and negative units.

    The split matters more than it looks. The profile carries an `Avoid:` line,
    and a plain max-cosine over every line would score a sports article *high*
    for a user who wrote "avoid sports" — the exact opposite of the intent, and a
    handicap the LLM does not have, since it reads the whole profile and
    understands the label. Negative units are therefore scored separately and
    subtracted.

    Mode A keeps each side as one block, mode B splits the topic lists into
    individual topics ("X, Y and Z" are three directions, not one).
    """
    lines = [ln.strip(" -•\t") for ln in profile.strip().splitlines() if ln.strip()]
    if max_lines:
        lines = lines[:max_lines]

    positive_lines, negative_lines = [], []
    for line in lines:
        label, _, topics = line.partition(":")
        # A line without a label is a plain sentence: keep it whole and positive.
        body = topics.strip() if topics.strip() else line
        target = (negative_lines if topics.strip() and _NEGATIVE_LABEL_RE.search(label)
                  else positive_lines)
        target.append(body)

    def units(group: list[str]) -> list[str]:
        if not group:
            return []
        if mode == "a":
            return ["\n".join(group)]
        out = [t for line in group for t in split_topics(line) if len(t) >= 4]
        return out or ["\n".join(group)]

    positive = units(positive_lines)
    if not positive:  # nothing recognised as positive: fall back to the raw text
        positive = [profile.strip()]
    if max_topics:
        # Cold-start ablation: a fresh account has a handful of interests, not a
        # generated profile with thirty. Cutting whole lines would not do it —
        # this profile has three, and dropping one only removes the avoid list.
        positive = positive[:max_topics]
    return positive, units(negative_lines)


# ── scoring ───────────────────────────────────────────────────────────────────

def embed(model, texts: list[str], prefix: str, batch_size: int) -> np.ndarray:
    return model.encode([prefix + t for t in texts],
                        normalize_embeddings=True,
                        batch_size=batch_size,
                        show_progress_bar=True)


def max_cosine(article_vecs: np.ndarray, profile_vecs: np.ndarray) -> np.ndarray:
    """Best match against any profile unit (vectors are normalized)."""
    return (article_vecs @ profile_vecs.T).max(axis=1)


def profile_score(article_vecs: np.ndarray, positive: np.ndarray,
                  negative: np.ndarray | None) -> np.ndarray:
    """Closeness to the best-matching interest, minus closeness to the avoid list."""
    score = max_cosine(article_vecs, positive)
    if negative is not None and len(negative):
        score = score - max_cosine(article_vecs, negative)
    return score


def tfidf_scores(article_texts: list[str], positive: list[str],
                 negative: list[str]) -> np.ndarray:
    """Lexical baseline: if embeddings cannot beat this, the sidecar is pointless.

    Fitted on the articles only. A profile word the corpus never uses then drops
    out, which is the honest behaviour: lexical matching cannot score what it has
    never seen. Negatives are subtracted the same way as for embeddings, so the
    two baselines differ only in how they represent text.
    """
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity

    vec = TfidfVectorizer(sublinear_tf=True, lowercase=True,
                          strip_accents="unicode", ngram_range=(1, 2),
                          min_df=2, max_features=200_000)
    articles = vec.fit_transform(article_texts)
    score = cosine_similarity(articles, vec.transform(positive)).max(axis=1)
    if negative:
        score = score - cosine_similarity(articles, vec.transform(negative)).max(axis=1)
    return score


# ── metrics ───────────────────────────────────────────────────────────────────

def paired_bootstrap(segments: list[dict], key_a: str, key_b: str,
                     n_iter: int, seed: int) -> dict:
    """CI for the pooled AUC difference (a − b), resampled within segments.

    Paired because both scorers see the same articles: resampling once and
    computing both AUCs keeps the shared variance out of the interval. Pooling
    weights each segment by its pair count (n_pos * n_neg), the natural weight
    for an AUC, because the segments differ in base rate.
    """
    rng = random.Random(seed)
    prepared = []
    for seg in segments:
        scores_a = seg["scores"][key_a]
        scores_b = seg["scores"][key_b]
        engaged = seg["engaged"]
        prepared.append((scores_a, scores_b, engaged, len(engaged)))

    deltas = []
    for _ in range(n_iter):
        num = den = 0.0
        for scores_a, scores_b, engaged, n in prepared:
            idx = [rng.randrange(n) for _ in range(n)]
            pairs_a = [(scores_a[i], engaged[i]) for i in idx]
            pairs_b = [(scores_b[i], engaged[i]) for i in idx]
            auc_a, auc_b = compute_auc(pairs_a), compute_auc(pairs_b)
            if auc_a is None or auc_b is None:
                continue
            n_pos = sum(1 for _, e in pairs_a if e)
            weight = n_pos * (n - n_pos)
            num += weight * (auc_a - auc_b)
            den += weight
        if den:
            deltas.append(num / den)
    deltas.sort()
    if not deltas:
        return {"n_iter": 0}
    return {
        "n_iter": len(deltas),
        "mean": sum(deltas) / len(deltas),
        "ci_low": deltas[int(0.025 * len(deltas))],
        "ci_high": deltas[min(int(0.975 * len(deltas)), len(deltas) - 1)],
        "share_above_zero": sum(1 for d in deltas if d > 0) / len(deltas),
    }


def pooled_auc_diff(segments: list[dict], key_a: str, key_b: str) -> float | None:
    num = den = 0.0
    for seg in segments:
        engaged = seg["engaged"]
        auc_a = compute_auc(list(zip(seg["scores"][key_a], engaged)))
        auc_b = compute_auc(list(zip(seg["scores"][key_b], engaged)))
        if auc_a is None or auc_b is None:
            continue
        n_pos = sum(engaged)
        weight = n_pos * (len(engaged) - n_pos)
        num += weight * (auc_a - auc_b)
        den += weight
    return num / den if den else None


def operating_point(scores: list[float], engaged: list[bool], keep: int) -> dict:
    """Engagement rate in the top `keep` articles by score, i.e. what a filter sees.

    Volume-matched rather than threshold-matched: the embedding scale is
    arbitrary until calibration, so the comparable question is "of the same
    number of articles a score-70 filter lets through today, how many are
    actually engaging".
    """
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:keep]
    hits = sum(1 for i in order if engaged[i])
    base = sum(engaged) / len(engaged) if engaged else 0.0
    rate = hits / keep if keep else 0.0
    return {"keep": keep, "engaged": hits, "rate": rate,
            "lift": (rate / base) if base else None}


def binarize(scores: list[float]) -> tuple[list[bool], float, float]:
    """LLM score split at its own median, for the agreement metric.

    Returns the split share too: the LLM quantizes hard, so a median can sit on a
    heap of ties and produce a 30/70 split rather than 50/50. An agreement number
    read without that share is misleading.
    """
    median = float(np.median(scores))
    labels = [s > median for s in scores]
    return labels, median, (sum(labels) / len(labels) if labels else 0.0)


def detect_lang(text: str) -> str:
    import py3langid
    return py3langid.classify(text[:600])[0]


# ── main ──────────────────────────────────────────────────────────────────────

def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--model", default="intfloat/multilingual-e5-small")
    ap.add_argument("--prefix-family", default="e5", choices=sorted(PREFIXES))
    ap.add_argument("--profile-mode", default="b", choices=["a", "b"],
                    help="a = one vector per side, b = one vector per topic")
    ap.add_argument("--input", default="title300",
                    choices=["title", "title300", "full2000"])
    ap.add_argument("--profile-lines", type=int, default=None,
                    help="keep only the first N profile lines")
    ap.add_argument("--profile-topics", type=int, default=None,
                    help="keep only the first N positive topics (cold-start ablation)")
    ap.add_argument("--ignore-negatives", action="store_true",
                    help="drop the avoid list instead of subtracting it "
                         "(ablation: how much the negative side is worth)")
    ap.add_argument("--retention-margin-days", type=int, default=5,
                    help="safety margin below T1 for the retention cut; articles "
                         "older than (T1 - margin) are dropped as purge survivors")
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20260822)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--cache-dir", type=Path, default=None,
                    help="directory for cached article vectors (.npy)")
    ap.add_argument("--label", default=None, help="name for this run in results.json")
    ap.add_argument("--dump-scores", type=Path, default=None,
                    help="write per-article scores as CSV (input for calibration "
                         "and for eyeballing the top of the ranking)")
    args = ap.parse_args()

    meta, rows = load_sample(args.sample)
    rows, retention = drop_post_retention(meta, rows, args.retention_margin_days)
    if retention.get("dropped"):
        def pct(value: float | None) -> str:
            return "n/a" if value is None else f"{value:.1%}"
        print(f"dropped {retention['dropped']} articles older than "
              f"{retention['cutoff_days']} days (engaged rate there: "
              f"{pct(retention['dropped_engaged_rate'])}, in the kept sample: "
              f"{pct(retention['kept_engaged_rate'])})", file=sys.stderr)
    if not rows:
        raise SystemExit("no rows left after the retention cut")
    warnings = sample_warnings(meta, rows)
    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    segments = assign_segments(meta, rows)
    older = segments.pop("_older")

    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(args.model)
    query_prefix, passage_prefix = PREFIXES[args.prefix_family]

    # Article vectors are shared by every segment, so encode once for the whole
    # sample and slice afterwards.
    for i, row in enumerate(rows):
        row["_index"] = i
    texts = [article_text(r, args.input) for r in rows]
    cache_path = None
    if args.cache_dir:
        args.cache_dir.mkdir(parents=True, exist_ok=True)
        # The article ids go into the filename: a different sample of the same
        # size must not silently reuse another run's vectors.
        fingerprint = hashlib.sha1(
            ",".join(str(r["article_id"]) for r in rows).encode()).hexdigest()[:10]
        cache_path = (args.cache_dir /
                      f"emb-{slug(args.model)}-{args.input}-{fingerprint}.npy")
    if cache_path and cache_path.exists():
        vectors = np.load(cache_path)
        if vectors.shape[0] != len(texts):
            raise SystemExit(f"cache {cache_path} holds {vectors.shape[0]} vectors, "
                             f"sample has {len(texts)}; delete it and rerun")
        print(f"loaded {vectors.shape} from {cache_path}", file=sys.stderr)
    else:
        vectors = embed(model, texts, passage_prefix, args.batch_size)
        if cache_path:
            np.save(cache_path, vectors)

    run = {
        "run": {
            "label": args.label or f"{args.model}|{args.input}|profile-{args.profile_mode}",
            "model": args.model,
            "input": args.input,
            "profile_mode": args.profile_mode,
            "profile_lines": args.profile_lines,
            "profile_topics": args.profile_topics,
            "ignore_negatives": args.ignore_negatives,
            "prefix_family": args.prefix_family,
            # Input longer than the model's window is silently truncated, which is
            # the whole question behind the `full2000` variant.
            "max_seq_length": getattr(model, "max_seq_length", None),
            "bootstrap": args.bootstrap,
            "seed": args.seed,
        },
        "sample": {
            "file": str(args.sample),
            "exported_at": meta.get("exported_at"),
            "user_id": meta.get("user_id"),
            "total_rows": len(rows),
            "older_than_p2": len(older),
            "counts": meta.get("counts"),
            "profile_regenerations": meta.get("profile_regenerations"),
            "retention_cut": retention,
            "warnings": warnings,
        },
        "segments": {},
    }

    def score_segment(name: str, seg_rows: list[dict], profile: str) -> dict:
        positive, negative = parse_profile(profile, args.profile_mode,
                                           args.profile_lines, args.profile_topics)
        if args.ignore_negatives:
            negative = []
        pos_vecs = embed(model, positive, query_prefix, args.batch_size)
        neg_vecs = (embed(model, negative, query_prefix, args.batch_size)
                    if negative else None)
        idx = [r["_index"] for r in seg_rows]
        return {
            "name": name,
            "scores": {
                "embedding": profile_score(vectors[idx], pos_vecs, neg_vecs).tolist(),
                "llm": [r["ai_score"] for r in seg_rows],
                "tfidf": tfidf_scores([texts[i] for i in idx], positive, negative).tolist(),
            },
            "engaged": [bool(r["engaged"]) for r in seg_rows],
            "rows": seg_rows,
            "profile_chars": len(profile),
            "profile_positive_units": positive,
            "profile_negative_units": negative,
        }

    def segment_report(scored: dict) -> dict:
        emb = scored["scores"]["embedding"]
        llm = scored["scores"]["llm"]
        tfidf = scored["scores"]["tfidf"]
        engaged = scored["engaged"]
        n_pos = sum(engaged)
        llm_labels, median, share = binarize(llm)
        report = {
            "profile_chars": scored["profile_chars"],
            # The parsed units are kept verbatim: a profile that parses into
            # nonsense (a label matched as negative, a topic list split at the
            # wrong comma) is the first thing to suspect in a bad result.
            "profile_positive_units": scored["profile_positive_units"],
            "profile_negative_units": scored["profile_negative_units"],
            "n": len(engaged),
            "engaged": n_pos,
            "engaged_rate": n_pos / len(engaged),
            "auc": {
                "llm": compute_auc(list(zip(llm, engaged))),
                "embedding": compute_auc(list(zip(emb, engaged))),
                "tfidf": compute_auc(list(zip(tfidf, engaged))),
            },
            "agreement_auc_vs_llm": {
                "llm_median": median,
                "share_above_median": share,
                "embedding": compute_auc(list(zip(emb, llm_labels))),
                "tfidf": compute_auc(list(zip(tfidf, llm_labels))),
            },
            "operating_points": {},
            "llm_calibration": calibration_buckets(list(zip(llm, engaged))),
            "llm_histogram": score_histogram(llm),
        }
        # Volume-matched operating points: as many articles as today's filters at
        # 0.6 and 0.7 let through, which is what a user actually sees.
        for threshold in (0.6, 0.7):
            keep = sum(1 for s in llm if s >= threshold)
            if keep == 0:
                continue
            report["operating_points"][f"llm>={threshold}"] = {
                "llm": operating_point(llm, engaged, keep),
                "embedding": operating_point(emb, engaged, keep),
                "tfidf": operating_point(tfidf, engaged, keep),
            }
        return report

    scored_segments = []
    for name in ("P2", "P3"):
        seg = segments[name]
        if not seg["rows"]:
            continue
        scored = score_segment(name, seg["rows"], seg["profile"])
        scored_segments.append(scored)
        run["segments"][name] = segment_report(scored) | {
            "start": seg["start"].isoformat(),
            "end": seg["end"].isoformat() if seg["end"] else None,
        }

    if not scored_segments:
        raise SystemExit("no segment held any rows")

    run["pooled"] = {
        "auc_diff_embedding_minus_llm": pooled_auc_diff(scored_segments, "embedding", "llm"),
        "auc_diff_embedding_minus_tfidf": pooled_auc_diff(scored_segments, "embedding", "tfidf"),
        "bootstrap_embedding_minus_llm": paired_bootstrap(
            scored_segments, "embedding", "llm", args.bootstrap, args.seed),
        "bootstrap_embedding_minus_tfidf": paired_bootstrap(
            scored_segments, "embedding", "tfidf", args.bootstrap, args.seed + 1),
    }

    # Control run: the whole exported window against today's profile, including
    # the rows older than P2 whose profile text is gone. Circular in a way the
    # clean segments are not (part of this window fed the profile it is scored
    # against), so it is a sanity check on direction, not a result: if the
    # difference points the same way here, the circularity is empirically small.
    if len(rows) > sum(len(s["engaged"]) for s in scored_segments):
        control = score_segment("control", rows, meta["profile"]["current"])
        run["control"] = segment_report(control) | {
            "note": "whole window against the current profile; circular, "
                    "direction check only",
        }

    # Language split: a secondary view, not a decision criterion (Czech feeds
    # carry different topics than English ones, so this confounds language
    # with subject matter).
    by_lang = defaultdict(lambda: {"embedding": [], "llm": [], "engaged": []})
    for seg in scored_segments:
        for i, row in enumerate(seg["rows"]):
            lang = detect_lang(f"{row['title']} {row['body']}")
            bucket = by_lang[lang]
            bucket["embedding"].append(seg["scores"]["embedding"][i])
            bucket["llm"].append(seg["scores"]["llm"][i])
            bucket["engaged"].append(seg["engaged"][i])
    run["by_language"] = {
        lang: {
            "n": len(b["engaged"]),
            "engaged": sum(b["engaged"]),
            "auc_llm": compute_auc(list(zip(b["llm"], b["engaged"]))),
            "auc_embedding": compute_auc(list(zip(b["embedding"], b["engaged"]))),
        }
        for lang, b in sorted(by_lang.items(), key=lambda kv: -len(kv[1]["engaged"]))
        if len(b["engaged"]) >= 50
    }

    args.out.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.dump_scores:
        with args.dump_scores.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["segment", "article_id", "scored_at", "llm", "embedding",
                             "tfidf", "engaged", "title"])
            for seg in scored_segments:
                for i, row in enumerate(seg["rows"]):
                    writer.writerow([
                        seg["name"], row["article_id"], row.get("scored_at"),
                        seg["scores"]["llm"][i], f"{seg['scores']['embedding'][i]:.6f}",
                        f"{seg['scores']['tfidf'][i]:.6f}", int(seg["engaged"][i]),
                        row["title"],
                    ])
        print(f"per-article scores written to {args.dump_scores}", file=sys.stderr)

    # Console summary, so a run is readable without opening the JSON.
    print(f"\n=== {run['run']['label']} ===")
    for name, rep in run["segments"].items():
        print(f"{name}: n={rep['n']} engaged={rep['engaged']} "
              f"({rep['engaged_rate']:.1%})  AUC llm={rep['auc']['llm']:.3f} "
              f"emb={rep['auc']['embedding']:.3f} tfidf={rep['auc']['tfidf']:.3f}")
    boot = run["pooled"]["bootstrap_embedding_minus_llm"]
    print(f"pooled AUC(emb) - AUC(llm) = "
          f"{run['pooled']['auc_diff_embedding_minus_llm']:+.3f} "
          f"[{boot['ci_low']:+.3f}, {boot['ci_high']:+.3f}]")
    boot_t = run["pooled"]["bootstrap_embedding_minus_tfidf"]
    print(f"pooled AUC(emb) - AUC(tfidf) = "
          f"{run['pooled']['auc_diff_embedding_minus_tfidf']:+.3f} "
          f"[{boot_t['ci_low']:+.3f}, {boot_t['ci_high']:+.3f}]")
    if "control" in run:
        ctl = run["control"]
        print(f"control (whole window, current profile): n={ctl['n']} "
              f"engaged={ctl['engaged']}  AUC llm={ctl['auc']['llm']:.3f} "
              f"emb={ctl['auc']['embedding']:.3f} tfidf={ctl['auc']['tfidf']:.3f}")
    print(f"written to {args.out}")


if __name__ == "__main__":
    main()

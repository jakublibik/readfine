"""Rescore the September window with another LLM, against the Haiku scores in it.

The clean sample carries the score production wrote (Haiku 4.5). This sends the
same rows through the app's own `score_article`, so prompt, token cap and
parsing are the production ones and only the model changes. The endpoint is any
OpenAI-compatible one (a local Ollama by default), called as provider `custom`,
which is how the app talks to such an endpoint, `reasoning_effort=none`
included.

Rows and profile follow `run_terms_fidelity.windows`: segment P2 of the clean
sample, scored against the August profile. Haiku on those rows is the 0.729 in
the plan, and the run fails if it does not reproduce that.

Answers are cached per model in a JSONL file, so an interrupted run picks up
where it stopped.

`--profile terms` and `--text fetch` swap in what the basic scorer reads (the
frozen `terms_v1` list, and the title plus the head of the feed description from
the corpus export), so the LLM can be compared with BM25 on the same input
rather than only as deployed. Use a separate cache per combination.

Run from `backend/`, where the app finds its `.env`:

    uv run --with numpy python ../scripts/embedding_eval/run_llm_rescore.py \\
        --sample sample_clean.jsonl --august-sample sample.jsonl \\
        --model qwen3:8b --cache rescore_qwen3_8b.jsonl
"""
import argparse
import asyncio
import gzip
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "backend"))

import run_eval  # noqa: E402
from app.services.ai_eval_service import compute_auc  # noqa: E402
from app.services.ai_jobs import normalize_content  # noqa: E402
from app.services.ai_service import score_article  # noqa: E402
from app.services import relevance_service as rs  # noqa: E402

HAIKU_P2_AUC = 0.729


def load_corpus(path: Path) -> dict[int, dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        next(fh)
        return {o["article_id"]: o for o in map(json.loads, fh)}


def read_terms(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def p2_rows(sample: Path, august_sample: Path) -> tuple[str, list[dict]]:
    august_meta, _ = run_eval.load_sample(august_sample)
    meta, rows = run_eval.load_sample(sample)
    # P2 was scored against the August profile, see step 0 of the plan.
    meta["profile"]["previous"] = august_meta["profile"]["current"]
    seg = run_eval.assign_segments(meta, rows)["P2"]
    return seg["profile"], seg["rows"]


async def db_key(user_id: int) -> str | None:
    from app import database
    from app.config import settings
    from app.services.ai_service import get_api_key
    engine = database.create_engine(settings.database_url)
    try:
        async with database.create_session_factory(engine)() as db:
            return await get_api_key(user_id, "anthropic", db)
    finally:
        await engine.dispose()


def load_cache(path: Path) -> dict[int, float | None]:
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                obj = json.loads(line)
                done[obj["article_id"]] = obj["score"]
    return done


async def rescore(rows, profile, client, model, cache: Path, concurrency: int,
                  texts: dict[int, str], provider: str) -> None:
    done = load_cache(cache)
    # A failed answer is cached as None and tried again on the next run.
    todo = [r for r in rows if done.get(r["article_id"]) is None]
    print(f"{len(done)} cached, {len(todo)} to score", file=sys.stderr)
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    started, finished = time.monotonic(), 0

    async def one(row):
        nonlocal finished
        content = texts[row["article_id"]]
        async with sem:
            try:
                score, _, _ = await score_article(content, profile, client, provider, model)
                error = None
            except Exception as exc:  # unparseable answer or timeout: record, go on
                score, error = None, f"{type(exc).__name__}: {exc}"[:300]
        async with lock:
            with cache.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"article_id": row["article_id"], "score": score,
                                     "error": error}) + "\n")
            finished += 1
            if finished % 50 == 0 or finished == len(todo):
                rate = finished / (time.monotonic() - started)
                left = (len(todo) - finished) / rate
                print(f"{finished}/{len(todo)}  {rate:.2f}/s  ~{left / 60:.0f} min left",
                      file=sys.stderr)

    await asyncio.gather(*(one(r) for r in todo))


def report(rows, cache: Path, model: str, n_iter: int, variant: str) -> dict:
    scores = load_cache(cache)
    missing = [r for r in rows if scores.get(r["article_id"]) is None]
    rows = [r for r in rows if scores.get(r["article_id"]) is not None]
    engaged = [bool(r["engaged"]) for r in rows]
    haiku = [r["ai_score"] for r in rows]
    other = [scores[r["article_id"]] for r in rows]
    seg = {"engaged": engaged, "scores": {"haiku": haiku, "other": other}}
    by_score = sorted(zip(other, engaged), key=lambda p: -p[0])
    out = {
        "model": model,
        "variant": variant,
        "rows": len(rows),
        "engaged": sum(engaged),
        "failed": len(missing),
        "auc_haiku": compute_auc(list(zip(haiku, engaged))),
        "auc_model": compute_auc(list(zip(other, engaged))),
        "diff_model_minus_haiku": run_eval.paired_bootstrap([seg], "other", "haiku",
                                                            n_iter, seed=1),
        "top100_hit_model": sum(e for _, e in by_score[:100]) / 100,
        "distinct_scores": len(set(other)),
    }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", required=True, type=Path)
    ap.add_argument("--august-sample", required=True, type=Path)
    ap.add_argument("--model", required=True)
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--limit", type=int, help="score only the first N rows (smoke run)")
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--profile", choices=("prose", "terms"), default="prose",
                    help="prose: the AI profile Haiku scored with; terms: terms_v1 EN+CS")
    ap.add_argument("--text", choices=("readable", "fetch"), default="readable",
                    help="readable: what the LLM scorer reads; fetch: what BM25 reads")
    ap.add_argument("--provider", choices=("custom", "anthropic"), default="custom",
                    help="custom: OpenAI-compatible --base-url; anthropic: as production")
    ap.add_argument("--key-file", type=Path, help="Anthropic key, else ANTHROPIC_API_KEY")
    ap.add_argument("--key-from-db", type=int, metavar="USER_ID",
                    help="the Anthropic key that user saved in the app's (dev) database")
    ap.add_argument("--terms-dir", type=Path)
    ap.add_argument("--corpus", type=Path)
    args = ap.parse_args()

    profile, rows = p2_rows(args.sample, args.august_sample)
    haiku = compute_auc([(r["ai_score"], bool(r["engaged"])) for r in rows])
    if round(haiku, 3) != HAIKU_P2_AUC:
        raise SystemExit(f"Haiku AUC on P2 is {haiku:.4f}, expected {HAIKU_P2_AUC}")

    if args.profile == "terms":
        if not args.terms_dir:
            raise SystemExit("--profile terms needs --terms-dir")
        # The deployed list, one term per line, as the reader enters it.
        profile = "\n".join(read_terms(args.terms_dir / "en.txt")
                            + read_terms(args.terms_dir / "cs_translations.txt"))
    if args.text == "fetch":
        if not args.corpus:
            raise SystemExit("--text fetch needs --corpus")
        corpus = load_corpus(args.corpus)
        rows = [r for r in rows if r["article_id"] in corpus]
        texts = {r["article_id"]: rs.article_text(r["title"], corpus[r["article_id"]]["body"])
                 for r in rows}
    else:
        texts = {r["article_id"]: normalize_content(r["title"], r["body"], 2000)
                 for r in rows}
    if args.limit:
        rows = rows[:args.limit]

    if args.provider == "anthropic":
        # Not from backend/.env: settings parse it with extra="forbid".
        if args.key_from_db:
            key = asyncio.run(db_key(args.key_from_db))
        elif args.key_file:
            key = args.key_file.read_text(encoding="utf-8").strip()
        else:
            key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise SystemExit("--provider anthropic needs --key-from-db, --key-file "
                             "or ANTHROPIC_API_KEY")
        from app.services.ai_service import _make_anthropic_client
        client = _make_anthropic_client(key)
    else:
        # Plain client rather than the app's custom one: that pins requests to public
        # addresses, and the local endpoint is exactly what it refuses.
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key="none", base_url=args.base_url, timeout=120)
    asyncio.run(rescore(rows, profile, client, args.model, args.cache, args.concurrency,
                        texts, args.provider))
    variant = f"provider={args.provider} profile={args.profile} text={args.text}"
    print(json.dumps(report(rows, args.cache, args.model, args.bootstrap, variant), indent=2))


if __name__ == "__main__":
    main()

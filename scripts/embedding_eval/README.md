# Embedding vs LLM scoring — offline test

Throwaway analysis tooling for the question "can a local embedding model replace
LLM relevance scoring". Not part of the app; nothing here runs in production.

The plan behind it lives outside this repo (`plans/readfine_embedding_offline_test_PLAN.md`).

## 1. Export the sample

Runs inside the app container, because it needs the app's config and the same
content normalization the scorer uses.

```bash
# on the server
cd ~/readfine
git fetch origin dev
git show origin/dev:scripts/embedding_eval/export_sample.py > /tmp/export_sample.py
docker cp /tmp/export_sample.py readfine-app-1:/tmp/export_sample.py
docker exec readfine-app-1 python /tmp/export_sample.py --user-id 1 --since 2026-06-15 \
    > ~/sample.jsonl
gzip ~/sample.jsonl
```

Then copy `sample.jsonl.gz` to the dev machine and delete both copies when the
test is finished. The file holds the interest-profile text, reading history and
article bodies, so it must not land in a synced directory or in git.

Output is JSONL: line 1 is a meta object (profile texts, profile-regeneration
timestamps, sample counts), every following line is one scored article.

## 2. Run the evaluation

```bash
uv run --script run_eval.py --sample /path/to/sample.jsonl --out results.json
```

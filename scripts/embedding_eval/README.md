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
uv run --script run_eval.py --sample /path/to/sample.jsonl --out results.json \
    --cache-dir ./cache --dump-scores scores.csv
```

Phase 1 defaults to `multilingual-e5-small`, one vector per profile topic, and
title + 300 characters of body. Other configurations are flags: `--model`,
`--input {title,title300,full2000}`, `--profile-mode {a,b}`, `--profile-lines N`
(cold-start ablation) and `--ignore-negatives`.

Notes on what the run does, because they are decisions and not details:

- **The sample is split by when the score was written**, not when the article
  arrived: a retroactively applied filter scores old articles against a newer
  profile, and splitting by arrival would compare a score to a profile that did
  not exist yet.
- **The avoid list is subtracted, not matched.** The profile has an `Avoid:`
  line, so a plain max-cosine over every line ranks a sports article *highest*
  for someone who wrote "avoid sports". Positive and negative units are scored
  separately and the negative side is subtracted; the TF-IDF baseline gets the
  same treatment so only the text representation differs. On the dev sample this
  one detail moves AUC from 0.31 to 0.85.
- **AUC lives inside a segment.** The profile regimes have different base rates,
  so only the difference is pooled (weighted by pair count) and the bootstrap
  resamples within segments.
- `--cache-dir` keeps the article vectors, keyed by model, input variant and the
  article ids, so repeated runs only re-encode the profile.

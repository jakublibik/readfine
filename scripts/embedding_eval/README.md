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
`--input {title,title300,full2000}`, `--profile-mode {a,b}`, `--profile-lines N`,
`--profile-topics N` (cold-start ablation), `--ignore-negatives`,
`--moderate-penalty SD`, `--aggregate {max,topk}` and `--exposure-floor SCORE`.

**Pass `--exposure-floor` whenever a filter with an `ai_score` condition also
does `mark_read` or `archive`.** Those articles never reached the unread list, so
their engagement label is an effect of the score under test and the LLM scores
against ground truth it wrote itself. On the production sample a `< 30 ->
mark_read` rule covered 49% of the articles (engagement rate there 0.09%) and
carried the LLM's AUC from 0.69 to 0.85. The flag adds a second set of metrics
without that band; report both, since the first is inflated by the manufactured
negative tail and the second deflated by the range restriction.

## 3. Scenario C: the articles nobody scored

Scoring is enqueued only when a non-AI filter applies a `label` action
(`filter_service.py:581`), so the set that gets an LLM score is the one keyword
and feed rules picked out, and roughly two thirds of what arrives gets nothing.
Whether a score would surface anything worth reading in there is a separate
question, and **it cannot be answered with an AUC**: the user is only ever shown
labeled articles, so engagement on the rest is zero by construction no matter how
good they are. It is decided by reading titles.

```bash
# on the server
docker exec readfine-app-1 python /tmp/export_sample.py --user-id 1 \
    --since 2026-07-01 --unscored --limit 1500 > ~/unscored.jsonl

# on the dev machine
uv run --script review_unscored.py prepare --sample unscored.jsonl \
    --out review.csv --key review_key.json
# fill the `want` column with y/n, without opening the key file
uv run --script review_unscored.py score --review review.csv --key review_key.json
```

The review list is built so that reading titles proves something:

- **Blind.** Which scorer picked which article lives in the key file, not in the
  list, so "the embedding's picks look good" cannot be wishful reading.
- **With a control.** Random articles from the same pool are mixed in. Without
  them "9 of 20 look interesting" means nothing, because nobody knows what 20
  random ones would have scored. Do not set `--control 0`.
- **Against BM25.** If word matching surfaces the same articles, the semantic
  model is not what found them, and the summary reports the embedding's
  hit rate on the picks BM25 missed separately for that reason.

A blank verdict is an error rather than a "no": a half-finished review would
otherwise read as "the scorers found nothing", which is the one wrong answer this
test could produce unnoticed.

## Notes

Notes on what the runs do, because they are decisions and not details:

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
- **BM25 is the baseline that decides the sidecar, not TF-IDF.** TF-IDF cosine
  is the weaker standard lexical ranker: no term-frequency saturation, and it
  normalises by vector length rather than document length, both of which hurt on
  queries as short as a profile topic. BM25 is what a search engine would use, so
  it is the honest thing to beat: a model container has to earn its RAM against
  the ranker you would otherwise get for free. Both share the tokenizer
  (lowercasing, accent stripping, 1-2 grams, `min_df=2`), so what separates them
  is the weighting scheme and nothing else. The run also reports BM25 against
  TF-IDF, since which of the two is "the lexical baseline" matters.
- **High and Moderate are two labels, not one.** The profile splits topics into
  "High relevance" and "Moderate relevance" lines, and the LLM honours the
  difference. The first round of runs did not: every positive topic went into one
  list and the score was a plain max, so a perfect match on a moderate topic beat
  a good match on a high one. `--moderate-penalty` discounts them, in standard
  deviations of the scorer's own similarities (embedding cosines sit near 0.8,
  TF-IDF near 0.05, so a penalty in raw points would mean different things to
  each). Default 0 reproduces the earlier runs.
- **`--aggregate topk` averages the k best topics instead of the single best.** A
  plain max grows with the number of topics, so a generated profile with 36 of
  them and a new account's three land on different scales. That does not hurt AUC
  within one profile, but it is exactly what the calibration step has to map onto
  a fixed 0..1 range.
- **The prefix convention is derived from the model name.** It used to default to
  e5 for everything, which would have quietly prepended `query: `/`passage: ` to a
  model never trained with them. `--prefix-family` still overrides.
- `--cache-dir` keeps the article vectors, keyed by model, input variant and the
  article ids, so repeated runs only re-encode the profile.

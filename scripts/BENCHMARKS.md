# Benchmarks

Two parts of Readfine guess at something a person could judge by eye: which part of a
page is the article, and whether two headlines are the same piece of news. Both have a
public corpus with a human-written ground truth, and both have a script here that scores
our code against it. This file records what the scripts measure and what they last said,
so a change can be compared against a number instead of an impression.

Neither runs in CI. The corpora are downloaded on first use into `.benchmark/`, which is
gitignored, and the numbers describe one version of the code at one moment: a change in
them is a reason to go and look, not a build failure.

## Readable extraction

```bash
uv run --project backend python scripts/benchmark_extraction.py
```

Corpus: Zyte's [article-extraction-benchmark](https://github.com/scrapinghub/article-extraction-benchmark),
181 news and blog pages with the article text written out by hand, MIT licensed. Scored
with that project's own token-level F1, so the output is comparable with the numbers it
publishes. `survey_extraction.py` is the other half of the pair: it fetches live pages
and flags shapes that look wrong, which is how an unknown failure gets noticed.

The corpus is news and blogs only. It answers "would this change hurt ordinary
articles" and says nothing about documentation, wikis or forums.

Last recorded finding: the heading-repair work (see the `0.9.x` entry in `CHANGELOG.md`)
changed the stored text of 1 page out of 181, and that one came out better.

## Story matching

```bash
uv run --project backend python scripts/benchmark_dedup.py
uv run --project backend python scripts/benchmark_dedup.py --cross-source-only --examples
uv run --project backend python scripts/benchmark_dedup.py --split train --sweep
# the embedding comparison pulls in torch on first run, which takes a while
uv run --with sentence-transformers --project backend python scripts/benchmark_dedup.py \
    --split train --embeddings intfloat/multilingual-e5-small
```

Corpus: [HLGD](https://github.com/tingofurro/headline_grouping) (Headline Grouping
Dataset, Laban et al., NAACL 2021), 20 056 headline pairs from 10 large news events,
each labelled by five annotators for whether the two headlines describe the same
underlying event. Its first two challenges are headline-only and headline-plus-date,
which is our matcher exactly: trigrams over the title inside a 72 h window.

`survey_dedup.py` is the other half of the pair and answers a different question. It
runs over our own articles and reports how much a threshold would catch, which is what
the shipped values were chosen from, but nobody ever said pair by pair which of those
really are the same story. The survey measures volume, this measures accuracy.

**Read the numbers with this in mind.** Both headlines in an HLGD pair come from the
same news event, so a negative is "two different moments of the Equifax breach", not "an
article about something else". That is the hard half of our problem and only the hard
half; the easy negatives that make up nearly all of our real traffic are not in there.
Precision below is a floor, and the absolute values do not transfer to our corpus. What
transfers is the comparison between approaches and the shape of the curve.

Reference points published with the dataset: human annotators reach about **0.90 F1**,
the best models in the paper **0.75**, and the Electra checkpoint the authors released
for "fine-tuned on HLGD + time" is named for its **0.74**.

### Results, 2026-09-14

"The app" is trigram similarity over the normalised title inside the 72 h window. All
three splits are reported because they disagree: HLGD is split by news event, so each
one covers only a handful of stories and a number from any single split moves by a lot.
Train is the largest and the one to believe when they conflict.

AUC, which is threshold-free and says how well an approach ranks the pairs:

| approach | train (15 492) | test (2 495) | dev (2 069) |
|---|---|---|---|
| word Jaccard | 0.844 | 0.772 | 0.758 |
| trigram, no window | 0.868 | 0.812 | 0.750 |
| **trigram + 72 h window (the app)** | **0.873** | **0.832** | **0.742** |
| IDF cosine | 0.873 | 0.836 | 0.819 |
| embedding cosine (multilingual-e5-small) | 0.918 | 0.893 | 0.759 |

Best F1 any threshold can reach, for the same five: word Jaccard 0.536 / 0.574 / 0.623,
trigram 0.556 / 0.615 / 0.610, trigram + window 0.562 / 0.618 / 0.606, IDF cosine
0.602 / 0.635 / 0.694, embeddings 0.653 / 0.715 / 0.618.

What the shipped thresholds do, trigram + window:

| | train | test | dev | test, cross-source only |
|---|---|---|---|---|
| 0.30, fold | P 0.586 / R 0.534 | P 0.730 / R 0.473 | P 0.672 / R 0.466 | P 0.706 / R 0.439 |
| 0.40, hide | P 0.801 / R 0.305 | P 0.919 / R 0.269 | P 0.842 / R 0.259 | P 0.896 / R 0.231 |
| 0.45 | P 0.864 / R 0.216 | P 0.975 / R 0.195 | P 0.889 / R 0.183 | P 0.964 / R 0.153 |

Cross-source only is the rule the app actually applies (15 % of HLGD pairs come from
one site and would never be compared). It barely moves anything, so the rest of the
table is quoted over all pairs.

### What it settled

- **The 0.40 hide threshold holds up.** Eight to nine times in ten (0.80 on train, 0.92
  on test) it fires on a pair a person also called the same story, and that
  is against the hardest negatives anyone has collected: both headlines are always about
  the same event already. This is the number the feature never had. The survey could say
  how many articles a threshold would hide, never how many of them deserved it.
- **The 72 h window is nearly free.** It costs 1 % of the recall on train and test and
  3 % on dev, and it improves AUC on both larger splits, so it removes more noise than
  signal.
- **0.30 is not the accuracy optimum, and that is fine.** Best F1 sits near 0.27 with
  precision around 0.60, and at 0.30 precision is 0.59 on train. Folding two rows
  together and hiding one are not the same bet, and the fold threshold is allowed to be
  the loose one: a wrong fold costs a reader one click.
- **0.45 looks tempting for hiding, on this corpus.** The precision curve is still
  climbing steeply there on every split: train 0.801 → 0.864, test 0.919 → 0.975, dev
  0.842 → 0.889, against a recall drop of about a third. That is the kind of trade an
  action that hides things should want. It did not survive contact with our own corpus;
  see below.
- **Recall is where a lexical matcher is weak.** At 0.40 it finds a quarter of the
  genuine pairs. Everything it misses just stays in the list as a separate article,
  which is the failure the feature is designed to tolerate.
- **The known failure mode is confirmed and is not a threshold problem.** The most
  confident false positive on the test split scores 0.677:

      Ivory Coast: Minister trapped after soldiers open fire
      Cote d'Ivoire: Minister Freed After Ivory Coast Soldiers Opened Fire

  Trapped and freed are two moments of one story, and no threshold separates them,
  because they are lexically more alike than most true pairs. This is the same shape as
  "Milo detained" against "Milo deported" (0.405) from our own corpus. It is the reason
  hiding is opt-in and the reason a hidden article is only marked read, never removed.
- **A second opinion on top of the trigram candidates is worth real money here.**
  Candidate generation has to stay on the GIN index, but the hide decision runs on a
  handful of pairs a day and could afford to score them again. Held at the same recall
  as trigram-at-0.40, a second score applied only to pairs the trigram already accepted
  at 0.30 gives:

  | | train | test | dev |
  |---|---|---|---|
  | trigram alone at 0.40 | P 0.801 (164 fp) | P 0.919 (14 fp) | P 0.842 (36 fp) |
  | + IDF cosine | P 0.852 (116 fp) | P 0.935 (11 fp) | P 0.898 (22 fp) |
  | + embedding cosine | P 0.863 (105 fp) | P 0.964 (6 fp) | P 0.826 (41 fp) |

  IDF cosine removes a fifth to two fifths of the wrong hides on every split and needs
  no model. Embeddings are better where they are better and worse on dev.
- **Title suffixes are a non-issue, and not for the reason this corpus suggests.** See
  the next section: our own feeds do not put them in titles at all.

### Checked against our own corpus, 2026-09-14

Every question the benchmark raised was then put to the production export the thresholds
were originally measured on (20 856 articles, 87 days, 23 feeds, cross-feed pairs inside
the 72 h window). All four came back "leave it alone", and the reasons are worth keeping,
because three of them are cases where HLGD's answer does not survive the trip.

To redo this, take a fresh export (the `\copy` in `survey_dedup.py`'s usage notes) and
run `survey_dedup.py --from-csv <export> --pairs pairs.tsv`, which writes every pair with
its score; the bands below are slices of that file. The export holds titles and read
state for real accounts, so it does not belong in the repository or in a synced folder.

**What we actually hide.** 537 cross-feed pairs reach 0.40 over the 87 days. 278 of them
(52 %) have byte-identical titles, and 237 of those share a normalised URL, which means
the existing URL dedup catches them first. The story matcher's own contribution is
roughly 300 pairs over 87 days, about 3.4 a day, and the interesting part of it is the
259 pairs whose titles genuinely differ.

**Precision on our data is far higher than on HLGD**, as predicted: on a random 25 of
those 259 non-identical pairs, 22 are the same story by my own reading, so about 0.88,
against 0.80 on HLGD train. Add the identical-title half, which is never wrong, and the
figure for everything hidden at 0.40 is around 0.94. That is one annotator on 25 pairs,
so treat it as an estimate, not a measurement: the dataset's own paper puts trained
human annotators at 0.90 F1 on this task.

1. **Do not move hiding to 0.45.** The [0.40, 0.45) band holds 108 of the 537 pairs, a
   fifth of everything hidden, and reading them they are almost all genuine: the same
   Nvidia acquisition, the same French social-media law, the same Hormuz statement, filed
   by two outlets. HLGD's gain at 0.45 comes from negatives built to be adversarial, both
   headlines drawn from one event on purpose. Our negatives are nothing like that, so the
   precision that threshold buys there is not on offer here, and the recall it costs is.
2. **Do not add IDF cosine as a second opinion.** It is a real win on HLGD, but it does
   not separate the pairs we actually get wrong. Of our three mislabelled pairs, it
   scores two of them 0.638 and 0.722 against a median of 0.574 for the correct ones, so
   it ranks them as *better* matches than typical true pairs.
3. **Do not add embeddings either.** Same test, same outcome: 0.912, 0.916 and 0.964 on
   the three wrong pairs, against a median of 0.946 and a 5th percentile of 0.901 for
   the correct ones. The model agrees with the trigram, and it is not being stupid: these
   pairs really are about the same topic.
4. **Title suffixes: nothing to fix.** 0 of our 23 feeds put an outlet name in the title,
   so `strip_feed_suffixes` is a no-op on our corpus and the shipped thresholds were
   measured on exactly the strings production compares. The discrepancy between the two
   code paths is real and harmless.

**The one thing that would move the needle is the thing nothing here can do.** All three
of our errors are the same shape: Google Maps renaming a lake against Apple Maps doing it
next, a press secretary resigning against a piece asking who replaces her. Every score
tried, character, word, IDF-weighted and semantic, calls these a match, and in the sense
each of them measures, they are. Telling them apart needs a notion of event identity
rather than similarity, which is where HLGD's own reported ceiling of 0.75 F1 comes from.
Until something addresses that, the right answers stay the ones already shipped: hiding
is opt-in, and a hidden article is marked read rather than removed.

### The follow-up cue list, 2026-09-15

A later idea, and the one thing so far that both corpora agreed was worth shipping:
words that mark a headline as a different piece rather than another account of the same
moment. It went through three forms.

**Learning the list from the data does not work.** Ranking tokens by how often a pair
carrying them on exactly one side turns out to be a different event pulls out
`commuting`, `commute`, `commutation`, `sentence`, `exit`, `repeal`, `irish`,
`landslide`. That is the vocabulary of HLGD's particular timelines, not of follow-ups,
and it proves it: a list learned on train fires on **zero** pairs in the test split.

**A wide hand-written list has a real but unusable signal.** Thirty-seven framing words
lift the not-same-event rate by 6.8, 8.9 and 13.7 points across the three splits, which
replicates but is not enough to act on: the base rate is 27-41 %, so even when a cue
fires the pair is still more likely than not to be the same story. As a veto it stopped
two to ten right decisions for every wrong one. Raising the threshold instead of vetoing
was about break-even at best, and never as good as simply moving the threshold.

**On our own corpus the picture is different, and for a structural reason.** HLGD's pairs
are curated inside one news event, so it contains no explainers; our feeds are full of
them. Splitting the wide list by word on our 537 hidden pairs concentrates the signal
almost entirely in the words that carry the framing themselves: `how` was right 7 times
in 10, `joins` 4 in 5, `why` 2 in 2, `replace` 1 in 1, against `amid` 3 in 7, `instead`
2 in 6, `more` 1 in 4, `latest` 1 in 4, `update` and `means` 0 in 1.

The short list of survivors stops 18 of our 537 hides, of which about 13 look right to
me, so roughly 40 % fewer wrong hides for about 1 % of the right ones. It catches two of
the three known errors. On HLGD it does almost nothing, which is what it should do on a
corpus with nothing to catch.

Shipped as `FOLLOW_UP_CUES` in `app/fetcher/stories.py`, applied to hiding only: folding
an explainer under the story it belongs to is useful, hiding it is not.

**The weakness to fix with the next export.** The list was chosen by reading the same 40
pairs it was then scored on, so the 13-of-18 is a fit to that sample and will be
optimistic. The mechanism is sound and the failure mode is the benign one (an article
stays in the list that could have gone), which is why it shipped before the confirmation
rather than after. Run it against a fresh export and record the honest number here.

### What the deployment found, 2026-09-19

Everything above measures whether two headlines are the same story. None of it measures
what a group does once it has more than two members, and that is where the shipped
version broke: on eight days of production it produced groups of **407 and 514
articles**, spanning ten days inside a 72 hour rule.

The corpus is the reason it was never seen. The thresholds were measured on an export of
23 feeds belonging to one account; production runs the grouping globally over 351 feeds,
and grouping is not a per-feed property. Where the survey said 11 articles a day out of
240 would collapse (4.6 %), production grouped about 1 500 out of 5 000 (30 %).

Rebuilding the similarity graph of the two large groups offline says plainly what they
were: of the 131 841 possible pairs inside the 514-member group, **1 341 were over the
threshold**, a density of 1 %, and plenty of members scored 0.000 against each other. It
was never a group, it was a chain. Single-link membership plus transitive merging is
enough on its own: each merge makes a group easier to match, so the next bridge is
likelier than the last.

Two ingredients fed it, and only the first is a matching problem:

1. **Template and periodic titles**, which are lexically near-identical and are not news:
   `New York Post - September 11, 2026` against `Grazia UK - 28 September 2026`,
   `eBay Coupons: 20% Off in September 2026`, `2026 09 19 HackerNews`, and model names
   like `Qwen3.8-Flash-Next-APEX-GGUF`. These match each other correctly and should not
   be compared at all.
2. **Non-English coverage**, where shared administrative phrasing carries a lot of
   trigrams (`В Архангельской области`), and the English-only cue list has nothing to
   say.

**Source suffixes were a false lead**, and the correction is worth recording because the
first write-up of this listed them as a third cause on the strength of one group held
together by ` - HuffPost`. Measured on the full export below, stripping them changes
120 815 pairs to 120 646, which is 0.14 %, and the survey's own detector finds a suffix
on 1 feed out of 269. Point 4 of the previous section stands after all: the discrepancy
between `title_norm` and `survey_dedup.py` is real and still harmless.

**What was changed** (see `app/fetcher/stories.py`): membership now requires matching at
least `MEMBERSHIP_SHARE` of a group's members rather than any one of them, groups never
merge through a shared article, and the window is measured from the group's root so a
group has a finite life. Replayed over the six worst groups in arrival order:

| rule | 514 members became | largest survivor |
|---|---|---|
| single-link + transitive (shipped) | 1 group | 514 |
| cap of 12 only | 196 groups | 12, full of unrelated articles |
| two links required | 283 groups | 46 |
| anchor to root | 286 groups | 22 |
| **half the group + root window** | **277 groups** | **15**, max span 70 h |

Pairs and triples are untouched by this, which matters because they are 2 693 of the
3 358 groups: half of a group of two is one member, which is the old rule exactly.

### The survey was measuring a different algorithm, 2026-09-19

Found while reading pair samples from the export below, and it invalidates every number
this file ever produced for a non-Latin feed. `survey_dedup.py` reimplements `pg_trgm`
so it can run against a CSV without a database, and its word split was `[^a-z0-9]+`,
which deletes Cyrillic and CJK outright. In a UTF-8 database `pg_trgm` treats both as
alphanumeric and keeps them.

The symptom: two unrelated Chinese forum posts sharing only the word "gpt" scored
**1.000** in the survey and **0.068** in Postgres. 42 % of the production corpus has a
title the old split mangles.

Fixed, and `--check-trgm` now scores sample pairs both ways and reports the drift,
sampling by script rather than uniformly, because a uniform sample of a mostly-Latin
corpus is how this survived. After the fix: mean drift 0.0001 over 399 pairs, nothing
off by more than 0.05. **Run it after touching normalisation**, and treat any number in
this file produced before this date on a multilingual corpus as unreliable.

One limit stays, measured rather than assumed: the token prefilter compares two articles
only if they share a significant token, and Chinese has no spaces, so a headline can be
one token. On a CJK-heavy slice the prefilter finds 16 pairs against 20 by brute force,
so it loses about **20 % of CJK pairs**. The absolute numbers are small, but the survey
undercounts that part of the corpus and Postgres does not.

### Confirmed on a full production export, 2026-09-19

62 240 articles, 269 feeds, 67 days, with no read state in it, so the file carries
nothing belonging to anybody. Run through `survey_dedup.py --from-csv`, which prints both
membership rules side by side (all figures post-fix):

| at 0.30 | transitive closure | half the group + root window |
|---|---|---|
| groups | 6 143 | 6 856 |
| largest group | **1 243** | 35 |
| folded away | — | 9 647 (144/day) |
| groups of 10+ | — | 18, holding 301 articles |

Production never showed a group of 1 243 only because the live path works forward in
windows rather than taking the closure of the whole corpus at once. 514 was the same
failure, caught early.

**What survives is mostly correct, which is the real news.** The largest remaining
groups are a 22-member Macklemore story across 8 feeds, a 21-member foldable iPhone
launch across 13, a 19-member Anthropic story across 15. That is the feature working.
Across 67 days only 18 groups reach 10 members at all, holding 301 articles between
them, so the fold is no longer where the damage is.

**What is left for step 2, and it is small.** Two feeds of AI model releases contribute
88 of those 301 articles (`AI & Trending now`, `AI & Tech Top day`, carrying names like
`DeepSeek-V4-Flash-0731-JANG-CRACK`), and the discussion boards V2EX and NodeSeek
another 39. Those are the only groups where the members are genuinely not one piece of
news. It is a handful of feeds rather than a scoring problem, so per-feed exclusion is
worth more here than any cleverer comparison.

**Hiding is untouched by all of this** and is worth keeping separate in the head. It is
pairwise at 0.40 against an article the reader read themselves, so the group rule never
enters into it. The ceiling on the export is 14 445 articles (216/day) with a pair over
0.40, but production hid 2 in a day, because it also needs the reader to subscribe to
both feeds and to have read the counterpart.

### Group membership: the second condition, 2026-09-20

The section above says the fold is no longer where the damage is. It is still where
*some* of it is, and this is the measurement of the rest. Same export as above (62 240
articles, 269 feeds, 13 July to 19 September), replayed with
`scripts/survey_story_groups.py`.

**The case that started it.** Production built a four-article group out of two stories:

```
240495 Forbes     Rep. Beatty Urges Court To Prevent Trump From Demolishing Kennedy Center  (root)
240846 LA Times   Is Trump demolishing the Kennedy Center? What you need to know about...   0.337 to the root
241199 Lifehacker What You Need to Know About Walmart's 'Fall Deals' Sale                    0.326 to the LA Times piece
252545 Wired      What You Need to Know About the Foreign-Made Router Ban in the US          0.354 / 0.349
```

The LA Times article is a legitimate member and also a bridge: it carries a stock
phrase, so it clears 0.30 against headlines it has nothing to do with. The share test
was satisfied at every step (1 of 2, then 2 of 3) because it counts matches and nothing
asks what the match was made of.

**A label-free way to count the damage.** A group holding a member pair that scores
under 0.15 was built by a chain, which is the defect itself rather than a proxy for it.
On the export, **42 groups holding 214 articles** fail that test. The worst read like
`Introducing the WIRED App || GNOME 51 释出` and `Mini EQ || pg-jev`.

**Four things that did not work**, all rejected on measurement:

| approach | why not |
|---|---|
| IDF cosine as a second opinion over the candidates | bands overlap: a boilerplate pair scores 0.221 against a correct pair's 0.239 |
| stock phrases learned from the corpus (n-grams across several feeds) | kills 6 right pairs to catch 5 wrong ones; `strait of hormuz` looks exactly like `need to know` |
| phrase burstiness (how many weeks a phrase recurs in) | boilerplate and a running story have the same statistics: 11 titles / 7 feeds / 7 weeks |
| mean similarity to the group **instead of** the share test | see the trap below |

**The trap, and the reason the rule has two conditions rather than one.** A mean counts
sub-threshold similarity (0.25 to 0.29) as evidence, which the share test ignores. That
is exactly what a template family is made of, so replacing one with the other trades one
failure for another:

| family | share test | mean 0.30 alone |
|---|---|---|
| `X проголосовал на выборах в Госдуму` | 1 member | 40 members |
| `College EA Meetups Everywhere Fall 2026` (35 universities) | 3 members | 35 members |
| magazine issues (`The Washington Post - September 11, 2026`) | 0 groups of 10+ | 11 groups, 283 articles |

**The sweep** (walking the corpus in timestamp order):

| rule | groups | grouped | lost | gained | incoherent | 10+ | of those date-template |
|---|---|---|---|---|---|---|---|
| share only | 6 856 | 16 536 | — | — | 42 (214) | 18 | 0 |
| mean 0.30 only | 6 844 | 16 947 | 138 | 549 | 5 (34) | 41 | 11 (283) |
| **share AND mean 0.30** | 6 906 | 16 463 | **121** | 48 | **3 (25)** | 19 | 0 |
| share AND mean 0.32 | 6 717 | 15 777 | 839 | 80 | 2 (18) | 18 | 0 |
| share AND mean 0.35 | 6 509 | 14 945 | 1 667 | 76 | 0 | 13 | 0 |

0.30 is the knee, not a round number: 0.32 costs seven times as many articles to save
one more group. The three that survive are mild (0.120 to 0.143, e.g. two articles about
the same Samsung update). The floor is held by the Kennedy case itself, where the Wired
headline comes out at 0.263.

**Order does not change the answer.** The replay above walks by timestamp; production
walks new ids in id order inside a fetch round. Re-run with `--order id`: 34 incoherent
groups (164 articles) become **3** (17 articles), for 141 lost and 60 gained. Different
absolute numbers, same conclusion, which is the point of checking.

> The first version of these id-order figures (36 → 1, 118 lost, 49 gained) was wrong
> and is corrected above. `find_pairs` orients each pair by timestamp, and 11 % of the
> pairs have the earlier article with the larger id, so an id-order walk over
> ts-oriented edges could decide an article before its counterpart and then decide it
> again on its own turn: 398 articles came out in two groups at once. The replay now
> files each edge under whichever article the walk reaches second and skips an article
> that is already placed, which is what `_link` does. The timestamp figures were never
> affected (there the two orders agree), so the sweep table above stands as published.

**Read by hand, because the metric is partly circular.** The incoherence metric
thresholds the same similarity the rule thresholds, so "42 down to 3" cannot stand on
its own. All 141 lost and all 60 gained decisions (`--dump-diff`, id order) were read:

- of the 141 lost, about **70 were right to lose** (date bridges, `X рассказала` and
  other template pairs, deal listings, "now available" and "what you need to know"
  phrases, the Kennedy case itself) and about **71 were genuine coverage** that no
  longer folds, e.g. `Emmy 2026, tutti i vincitori` next to `„Widow's Bay" und „The
  Pitt" räumen bei Emmys ab`
- of the 60 gained, about **34 are right**, and most of the rest are new two-member
  template pairs (`The Economist UK - September 12, 2026` with `The Week Junior UK - 12
  September 2026`), which are harmless in the sense that they do not grow

So the honest trade is roughly **71 correct folds given up to break 31 incoherent
groups**, about one a day against half a group a day. It is worth taking because the two
costs are not symmetric: a fold that does not happen shows the reader two rows instead of
one, while a wrong group shows them a story that is not a story, and can pull an unread
article under a headline they marked read. Hiding is not affected either way — it is
pairwise at 0.40 against something the reader read themselves.

**Blocking is not the reason for any of this.** `survey_dedup.py --check-blocking` on
the first 1 500 articles: the token prefilter finds 260 pairs against 262 by brute
force, 0.8 % lost. (The CJK caveat from 2026-09-19 still stands separately.)

**Measured on production, 2026-09-20.** Everything above is a replay. Run as SQL over
the live `articles.story_id` on the 10 days the regroup covered, before and after
`backfill_stories --days 10 --reset`: **21 incoherent groups become 2**, and the
articles in them go from roughly 80 to 100 down to exactly 11.

Three things that reading gives which the replay could not:

- **The replay understated the damage.** 42 groups over the export's 69 days is 0.6 a
  day; production had 21 over 10 days, which is 2.1. The replay builds every group in
  one ordered pass, while production's were built by the live fetcher batch by batch,
  and until this change it could also re-decide an article it had already grouped. So
  the survey models the rule, not the path that runs it, and the absolute counts in the
  tables above are the rule's numbers rather than production's.
- **The survivors are the same two groups.** Production came out holding stories
  224443 and 231728, which are two of the three the replay predicted. Agreeing on
  *which* groups survive is better evidence for the method than agreeing on how many.
- **Both survivors are correct groups**, so the real count after the change is zero. One
  is coverage of the iOS 27 release, the other the One UI 9 rollout; the metric flags
  them because one headline says "One UI 9" where another says "Android 17", which is
  one story written two ways rather than a chain. 0.15 is a floor for finding chains,
  and near it a legitimate group can trip it.

The query, which counts members rather than pairs (the obvious form of it groups the
self-join and so counts the pairs, which is what the 193 in the first reading was):

```sql
SELECT count(*) AS incoherent_groups, sum(n) AS articles FROM (
  SELECT story_id, count(*) AS n FROM articles
  WHERE story_id IN (
    SELECT a.story_id FROM articles a
    JOIN articles b ON b.story_id = a.story_id AND b.id > a.id
    WHERE a.story_id IS NOT NULL
      AND COALESCE(a.published_at, a.fetched_at) >= now() - interval '10 days'
      AND COALESCE(b.published_at, b.fetched_at) >= now() - interval '10 days'
    GROUP BY a.story_id
    HAVING min(similarity(a.title_norm, b.title_norm)) < 0.15
  )
  GROUP BY story_id
) g;
```

The window in it has to match `--days`, because `--reset` clears every group in the
database while the regroup only rebuilds the window. Compared over the whole table, part
of the drop would just be the old groups outside the window never coming back.

Shipped as `MEMBERSHIP_MEAN` in `app/fetcher/story_params.py`, alongside
`MEMBERSHIP_SHARE` and not instead of it. The earlier finding "do not add IDF cosine as a
second opinion" (2026-09-14) still stands and is about a different class of error: it was
measured on the hiding branch, where the mistakes are same-topic-different-event rather
than chains through a stock phrase.

### Same-feed members in the share test, 2026-09-24

Same-feed pairs are never candidates, but the share test divides by every member of
the group, own feed included. On production an article from an aggregator feed
(`AI & Trending now`) stayed out of a 5-member group because one member came from its
own feed: 2 matches out of 5 fails, 2 out of the 4 it could have matched would pass.

The obvious fix, counting the share only over members from other feeds, is
`survey_story_groups.py --eligible-share`. On the 62k-article whole-instance export of
2026-09-19, walked in id order:

| | shipped | other feeds only |
|---|---|---|
| articles grouped | 16,451 | 16,877 |
| groups at the 40 cap | 0 | 3 |
| date-template groups | 0 (0 articles) | 6 (159) |
| incoherent groups | 3 (17 articles) | 6 (73) |
| articles in a different group | | 1,026 |

It brings back the template families the share test was written against: a feed that
publishes one headline template many times stops counting against itself, and its group
grows the way groups did before. Not shipped. What the gap costs is an article ending up
in a smaller group of its own instead of the big one, which is minor.

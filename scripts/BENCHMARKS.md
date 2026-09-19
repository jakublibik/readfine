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

Three ingredients fed it, and only the first is a matching problem:

1. **Template and periodic titles**, which are lexically near-identical and are not news:
   `New York Post - September 11, 2026` against `Grazia UK - 28 September 2026`,
   `eBay Coupons: 20% Off in September 2026`, `2026 09 19 HackerNews`, and model names
   like `Qwen3.8-Flash-Next-APEX-GGUF`. These match each other correctly and should not
   be compared at all.
2. **Source suffixes in the title**, e.g. ` - HuffPost`. Point 4 of the section above
   recorded that 0 of our 23 feeds did this, which is why `title_norm` never stripped
   them while `survey_dedup.py` did. Across 351 feeds it is no longer true, and the
   discrepancy between the two code paths stopped being harmless.
3. **Non-English coverage**, where shared administrative phrasing carries a lot of
   trigrams (`В Архангельской области`), and the English-only cue list has nothing to
   say.

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

**Still open.** None of this addresses ingredient 1 or 2. Template titles form genuinely
dense clusters, so the membership rule only shrinks them: a group of 22 headed by
`Chewy Promo Codes: $20 Off September 2026` survives it. That needs the titles kept out
of matching in the first place, and the thresholds want re-measuring on a corpus that
looks like production rather than like one account.

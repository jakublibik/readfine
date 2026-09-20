"""The numbers cross-source story grouping is decided by, and nothing else.

Separate from ``stories.py`` because the same rule is implemented three times: in the
fetcher, in ``app.scripts.backfill_stories`` (which has to reach the same grouping the
live path would, or a second run reshuffles the first) and in ``scripts/survey_dedup.py``
(which has to measure the rule that actually runs, or its numbers describe nothing). The
survey used to copy the values by hand so it could run against a CSV without pulling in
SQLAlchemy, and a code review had to check by eye that the copies still matched. This
module imports nothing, so there is no copy to check.

Every value here was measured against a production export rather than chosen, and the
comment on each says what the measurement was. Changing one is not a tweak: the groups
already in the database were built by the old value, so it means a
``backfill_stories --reset`` regroup as well. See ``scripts/BENCHMARKS.md``.
"""

# Measured, not chosen: see scripts/survey_dedup.py and the plan behind it. At 0.30 the
# production corpus collapses ~11 articles a day out of ~240; the precision cliff sits
# between 0.25 and 0.30 and everything above 0.50 is effectively an exact-title match.
COLLAPSE_THRESHOLD = 0.30

# A story is a burst. Coverage three weeks apart is a new story about the same subject,
# and the window is measured from the group's root so a group cannot crawl forward by
# re-anchoring on its newest member at every fetch.
WINDOW_HOURS = 72

# Hiding an article outright is a stronger claim than folding it away, so it asks for a
# stronger match, and one made directly against the article the reader read rather than
# through the group. The same export puts this at ~2.4 articles a day hidden and 2 the
# reader would have wanted over 87 days. Below 0.40 that regret climbs fast.
SUPPRESS_THRESHOLD = 0.40

# Titles shorter than this are not compared at all. A trigram score over a handful of
# trigrams swings wildly, and the fetcher's own "Untitled" placeholder (rss.py) would
# otherwise group every title-less item in the database into one story.
MIN_TITLE_CHARS = 12

# How much of a group an article has to match to join it, as a fraction of its members.
# A half, which is the weakest rule that still says something about the group rather
# than about one lucky neighbour: it leaves pairs and triples exactly as they were (for
# a group of one or two, half of it is one member, which is the old rule) and bites from
# three members up, where the chains start. Measured on the six worst production groups:
# 514 members became 277 groups of at most 15.
MEMBERSHIP_SHARE = 0.5

# The second condition, and it holds *as well as* the share above, not instead of it.
# An article's mean similarity to every member of the group has to reach this.
#
# Why a second condition at all: the share test counts matches, so an article that
# genuinely belongs can also be a bridge. A production group held a Kennedy Center
# report, an LA Times piece about it headlined "...what you need to know about...", and
# then Walmart's Fall Deals and a router ban, both of which cleared 0.30 against the
# stock phrase alone. Every step satisfied the share test arithmetically (1/2, then 2/3).
#
# Why not instead of it: the mean counts sub-threshold similarity (0.25 to 0.29) as
# evidence, which is precisely what a template family is made of. Replacing the share
# test with the mean turned "X voted in the Duma elections" from 1 member into 40, a set
# of 35 university event announcements from 3 into 35, and created 11 groups of magazine
# issues held together by the date in the title. The two conditions fail in opposite
# directions, so both run.
#
# 0.30 is the knee of the sweep on a 62k-article production export, not a round number:
# it drops 121 articles out of 16 536 grouped and cuts groups holding a member pair
# under 0.15 from 42 to 3. At 0.32 the same trade costs 839 articles and saves one more
# group. See the "Group membership" section of scripts/BENCHMARKS.md.
MEMBERSHIP_MEAN = 0.30

# Runaway guard, not a mechanism. With the rules above the largest group measured on the
# production corpus was 35, so this should never fire; it is here because the failure it
# guards against was a 514-member group that nothing noticed for a week. It fires
# silently in the sense that the 41st article simply stays on its own, so the fetcher
# logs a warning when it does.
MAX_GROUP_SIZE = 40

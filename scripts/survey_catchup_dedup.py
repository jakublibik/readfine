#!/usr/bin/env python
"""Measure whether folding stories into a digest would change what it says.

Catch me up and the briefings know nothing about story grouping, so five outlets
covering one event arrive as five lines of the prompt. Whether that is worth fixing
cannot be answered from the corpus-wide duplicate rate (~4.7 % of articles at the 0.30
threshold), because a digest is not a random sample of the window: it is the top N by
score, and duplicates are correlated in score. Five articles about one event score
alike against one profile, so if one gets in, the rest probably do too, and duplicates
are over-represented in the output by some unknown factor. This measures that factor.

Read-only, and deliberately not a pytest test: the numbers describe one account at one
moment, and a change in them is a reason to go and look rather than a build failure.

It imports the live ``catchup_service`` and measures through ``fetch_catchup_articles``
and ``apply_catchup_limit`` as they are, rather than reimplementing the sampling. The
two columns the service does not select yet (``story_id`` and whether the article has a
readable body) are fetched here by id, so this needs no production change at all.

Usage, from the repository root against the local database::

    uv run --project backend python scripts/survey_catchup_dedup.py --user 1
    uv run --project backend python scripts/survey_catchup_dedup.py --config 3

Against production, where the image carries ``backend/`` but not ``scripts/``, copy it
in first (it lands in a tmpfs, nothing is written to the image)::

    docker compose cp scripts/survey_catchup_dedup.py app:/tmp/
    docker compose exec app python /tmp/survey_catchup_dedup.py --config 3

``--config`` takes a saved catch-up config with its scope, labels, score floor and
limit, which is the only way to measure what an account actually runs rather than what
this script guessed. Without it the parameters come from the flags, whose defaults
match the form's.

What the report answers, and the rule it was written against (see the plan,
``readfine_story_catchup_PLAN.md``, step 0):

  M1 >= 5 %                  build folding: duplicates are visible in the digest
  M1 < 5 % and M3 >= 10 %    build only the marker, counting the whole group the
                             reader could open: nothing to fold, but "four other
                             outlets ran this" still says something about weight
  both below                 build neither, keep the hidden-article fix, write the
                             numbers down and revisit

A briefing scoped to a single feed reports M1 near zero whatever the corpus does,
because grouping only ever pairs articles across feeds. That is a property of the
scope, not evidence about the feature, so the report says so and M3 is what to read.

One account is one account. The grouping is global and a group's size depends on how
many feeds could have joined it, so a larger instance would score higher here, not
lower: a verdict of "do not build" is the safe direction to be wrong in.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path


_ROOTS = [Path(__file__).resolve().parent.parent / "backend", Path("/app")]


def _bootstrap_path() -> None:
    """Put the application on sys.path, from a checkout or from inside the image.

    Running as ``python /tmp/survey_catchup_dedup.py`` puts /tmp on the path and
    nothing else, so the import of ``app`` has to be arranged before it happens.
    """
    for root in _ROOTS:
        if (root / "app" / "config.py").exists():
            sys.path.insert(0, str(root))
            return
    raise SystemExit("Cannot find the application package (looked in ./backend and /app)")


def database_url(explicit: str | None) -> str:
    """Where to connect, without importing app.config.

    ``Settings`` resolves its .env relative to the working directory, so importing it
    from a script run at the repository root fails on every required field. The url is
    all this needs, and in the container it is in the environment already.
    """
    import os

    if explicit:
        return explicit
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    for root in _ROOTS:
        env = root / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("DATABASE_URL="):
                    return line.split("=", 1)[1].strip()
    raise SystemExit("No database url: pass --database-url or set DATABASE_URL")


_bootstrap_path()

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from app.database import create_engine, create_session_factory  # noqa: E402
from app.models.article import Article, UserArticleState  # noqa: E402
from app.models.user import UserCatchupConfig, UserSettings  # noqa: E402
from app.services.article import (  # noqa: E402
    add_article_access_joins,
    article_access_predicate,
)
from app.services.scope_tokens import parse_scope_tokens  # noqa: E402
from app.services.catchup_service import (  # noqa: E402
    CatchupArticle,
    apply_catchup_limit,
    fetch_catchup_articles,
)
# Private on purpose over there, borrowed on purpose here: the day an article falls on
# decides which bucket the sampling puts it in, and a second copy of that rule would be
# the one thing in this script that could disagree with production.
from app.services.catchup_service import _ts  # noqa: E402

# The thresholds the verdict is read against. Written down before the first run, so the
# report states a decision rather than inviting one to be argued backwards out of it.
#
# M1 gates folding: how much of the digest is the same news twice.
# M3 gates the marker: how much of it stands on coverage the reader has elsewhere. It
# is deliberately not M2 (the in-scope count), because a briefing scoped to one feed
# has an M2 of nearly zero by construction rather than by evidence, and the marker is
# the one part of this that still means something there.
M1_BUILD_FOLDING = 5.0
M3_BUILD_MARKER = 10.0


@dataclass
class Params:
    """One digest as it would actually run."""

    label: str
    user_id: int
    tz_str: str
    period: str
    scope_include: str | None
    filter_status: str
    label_filter: str | None
    filter_score_min: float | None   # 0.0-1.0, as the config stores it
    article_limit: int
    scoring_available: bool


@dataclass
class Facts:
    """The per-article columns the catchup projection does not carry yet.

    ``visible_members`` is keyed by story and counts the whole group this reader could
    open, subscription and retention applied, regardless of the digest's own scope.
    That is a different number from "how many of them are in this digest", and the
    difference is the whole question when a briefing is scoped to one feed: grouping
    only ever pairs articles across feeds, so inside one feed there is nothing to fold,
    while "four other outlets ran this" is still true and still worth saying.
    """

    story: dict[int, int | None]
    readable: set[int]
    hidden: set[int]
    visible_members: dict[int, int]


@dataclass
class Report:
    window: int
    window_grouped: int
    sampled: int
    dupe_rows: int
    rows_with_sources: int
    rows_with_visible_sources: int
    new_entrants: int
    hidden_in_sample: int
    single_feed_scope: bool

    @property
    def m1(self) -> float:
        return 100.0 * self.dupe_rows / self.sampled if self.sampled else 0.0

    @property
    def m2(self) -> float:
        return 100.0 * self.rows_with_sources / self.sampled if self.sampled else 0.0

    @property
    def m3(self) -> float:
        return 100.0 * self.rows_with_visible_sources / self.sampled if self.sampled else 0.0

    @property
    def verdict(self) -> str:
        if self.m1 >= M1_BUILD_FOLDING:
            return "fold + marker (B)"
        if self.m3 >= M3_BUILD_MARKER:
            return "marker only, counting the whole group (A')"
        return "neither (C)"


async def load_facts(ids: list[int], user_id: int, db: AsyncSession) -> Facts:
    """story_id, "has a readable body" and "was kept out" for the given articles.

    ``readable_content IS NOT NULL`` rather than its length: a length() would detoast
    every body in the window, which is the cost the light projection in
    fetch_catchup_articles exists to avoid.
    """
    if not ids:
        return Facts({}, set(), set(), {})

    rows = (await db.execute(
        select(Article.id, Article.story_id, Article.readable_content.isnot(None))
        .where(Article.id.in_(ids))
    )).all()
    hidden = set((await db.execute(
        select(UserArticleState.article_id).where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id.in_(ids),
            UserArticleState.hidden_at.is_not(None),
        )
    )).scalars().all())

    # The same access rule the reader footer uses (story_service._members_query), asked
    # once for every story on the page instead of once per article.
    story_ids = {r[1] for r in rows if r[1] is not None}
    visible: dict[int, int] = {}
    if story_ids:
        visible = {
            row[0]: row[1]
            for row in (await db.execute(
                add_article_access_joins(
                    select(Article.story_id, func.count(Article.id)), user_id
                )
                .where(
                    Article.story_id.in_(story_ids),
                    Article.trimmed_at.is_(None),
                    article_access_predicate(),
                )
                .group_by(Article.story_id)
            )).all()
        }
    return Facts(
        story={r[0]: r[1] for r in rows},
        readable={r[0] for r in rows if r[2]},
        hidden=hidden,
        visible_members=visible,
    )


def _key(article: CatchupArticle, facts: Facts) -> int:
    """One key per story, one per article that has none.

    The same arithmetic as story_service.row_count: ids are positive, so negating an
    ungrouped article's own id can never collide with a story id.
    """
    story = facts.story.get(article.id)
    return story if story is not None else -article.id


def fold(articles: list[CatchupArticle], facts: Facts) -> tuple[list[CatchupArticle], dict[int, int]]:
    """Keep one article per story; return the survivors and how many each stands for.

    The representative rule under test: highest score, then the one with a readable
    body (the snippet is most of what the model sees, and the first report of an event
    is often the wire stub that extraction failed on), then the oldest, which is the
    first account of the event and does not change when more coverage arrives.
    """
    groups: dict[int, list[CatchupArticle]] = {}
    for a in articles:
        groups.setdefault(_key(a, facts), []).append(a)

    kept: list[CatchupArticle] = []
    folded: dict[int, int] = {}
    for members in groups.values():
        best = min(
            members,
            key=lambda a: (
                -(a.ai_score if a.ai_score is not None else -1),
                0 if a.id in facts.readable else 1,
                _ts(a),
            ),
        )
        kept.append(best)
        folded[best.id] = len(members) - 1
    # Back into the order fetch_catchup_articles handed them over in, so the sampling
    # downstream sees what it would see in production.
    order = {a.id: i for i, a in enumerate(articles)}
    kept.sort(key=lambda a: order[a.id])
    return kept, folded


async def survey(params: Params, db: AsyncSession) -> Report:
    articles = await fetch_catchup_articles(
        user_id=params.user_id,
        tz_str=params.tz_str,
        db=db,
        period=params.period,
        scope_include=params.scope_include,
        filter_status=params.filter_status,
        label_filter=params.label_filter,
        filter_score_min=params.filter_score_min,
    )
    facts = await load_facts([a.id for a in articles], params.user_id, db)

    # What runs today.
    sampled = apply_catchup_limit(articles, params.article_limit, params.scoring_available)
    seen: set[int] = set()
    dupes = 0
    for a in sampled:
        k = _key(a, facts)
        if k in seen:
            dupes += 1
        seen.add(k)

    # What would run with folding in front of the sampling.
    folded_articles, folded_counts = fold(articles, facts)
    sampled_folded = apply_catchup_limit(
        folded_articles, params.article_limit, params.scoring_available
    )
    today_ids = {a.id for a in sampled}

    def has_visible_others(a: CatchupArticle) -> bool:
        story = facts.story.get(a.id)
        # The article itself is one of the visible members, so a group of one is a
        # story nobody else covered — or whose coverage this reader cannot open.
        return story is not None and facts.visible_members.get(story, 0) > 1

    feed_ids, folder_ids = parse_scope_tokens(params.scope_include)

    return Report(
        window=len(articles),
        window_grouped=sum(1 for a in articles if facts.story.get(a.id) is not None),
        sampled=len(sampled),
        dupe_rows=dupes,
        rows_with_sources=sum(1 for a in sampled_folded if folded_counts.get(a.id, 0) > 0),
        rows_with_visible_sources=sum(1 for a in sampled if has_visible_others(a)),
        new_entrants=sum(1 for a in sampled_folded if a.id not in today_ids),
        hidden_in_sample=sum(1 for a in sampled if a.id in facts.hidden),
        single_feed_scope=len(feed_ids) == 1 and not folder_ids,
    )


def print_report(params: Params, r: Report) -> None:
    print(f"\n{params.label}  ·  period={params.period}  limit={params.article_limit}"
          f"  scoring={'on' if params.scoring_available else 'off'}")
    if not r.sampled:
        print("  nothing in the window")
        return
    print(f"  window                  {r.window:5d} articles, {r.window_grouped} in a group")
    print(f"  sampled today           {r.sampled:5d} rows")
    print(f"  M1 duplicate rows       {r.dupe_rows:5d}  ({r.m1:.1f} %)   "
          f"[>= {M1_BUILD_FOLDING:.0f} % → fold]")
    print(f"  M2 folded rows          {r.rows_with_sources:5d}  ({r.m2:.1f} %)   "
          f"[in scope only, diagnostic]")
    print(f"  M3 rows with coverage   {r.rows_with_visible_sources:5d}  ({r.m3:.1f} %)   "
          f"[>= {M3_BUILD_MARKER:.0f} % → marker]")
    print(f"  new entrants            {r.new_entrants:5d}  articles folding would let in")
    print(f"  kept out of unread      {r.hidden_in_sample:5d}  in the sample "
          f"(the hidden_at fix, measured separately)")
    if r.single_feed_scope:
        print("  note: scope is a single feed, so M1 and M2 are near zero by "
              "construction — grouping only pairs articles across feeds. Read M3.")
    print(f"  → {r.verdict}")


async def params_from_config(config_id: int, db: AsyncSession) -> Params:
    config = await db.get(UserCatchupConfig, config_id)
    if config is None:
        raise SystemExit(f"No catch-up config with id {config_id}")
    return await _params(
        user_id=config.user_id,
        label=f'config "{config.name}"',
        period=config.period,
        scope_include=config.scope_include,
        filter_status=config.filter_status,
        label_filter=config.label_filter,
        # Configs store 0.0-1.0; the form divides by 100 before saving.
        filter_score_min=config.filter_score_min,
        article_limit=config.article_limit,
        db=db,
    )


async def _params(
    user_id: int, label: str, period: str, scope_include: str | None,
    filter_status: str, label_filter: str | None, filter_score_min: float | None,
    article_limit: int, db: AsyncSession,
) -> Params:
    s = (await db.execute(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )).scalar_one_or_none()
    return Params(
        label=label,
        user_id=user_id,
        tz_str=(s.timezone if s else None) or "UTC",
        period=period,
        scope_include=scope_include,
        filter_status=filter_status,
        label_filter=label_filter,
        filter_score_min=filter_score_min,
        article_limit=article_limit,
        # The same expression briefing_service uses to decide the coverage ratio.
        scoring_available=bool(s and s.ai_scoring_enabled_default),
    )


async def run(args) -> None:
    engine = create_engine(database_url(args.database_url))
    factory = create_session_factory(engine)
    try:
        async with factory() as db:
            if args.config:
                base = await params_from_config(args.config, db)
            else:
                if not args.user:
                    raise SystemExit("Pass --user or --config")
                base = await _params(
                    user_id=args.user,
                    label=f"user {args.user}",
                    period="today",
                    scope_include=None,
                    filter_status=args.status,
                    label_filter=args.label_filter,
                    # The flag takes the 0-100 of the form, the service wants 0.0-1.0.
                    filter_score_min=(args.score_min / 100 if args.score_min is not None else None),
                    article_limit=args.limit,
                    db=db,
                )

            periods = args.periods.split(",") if args.periods else [base.period]
            for period in periods:
                params = Params(**{**base.__dict__, "period": period.strip()})
                print_report(params, await survey(params, db))
            print()
    finally:
        await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=int, help="measure a saved catch-up config by id")
    ap.add_argument("--user", type=int, help="measure this user with the flags below")
    ap.add_argument("--periods", help="comma separated, e.g. today,7days "
                                      "(default: today,7days, or the config's period)")
    ap.add_argument("--limit", type=int, default=200, help="article limit (default 200)")
    ap.add_argument("--status", default="all", choices=["all", "not_opened"])
    ap.add_argument("--label-filter", help='JSON, e.g. ["any"] or ["label:3"]')
    ap.add_argument("--score-min", type=float, help="score floor, 0-100 as in the form")
    ap.add_argument("--database-url", help="override DATABASE_URL")
    args = ap.parse_args()
    # With --config and no --periods the config's own period is the point of the run;
    # without a config there is nothing to inherit, so measure both ends of the range.
    if not args.periods and not args.config:
        args.periods = "today,7days"
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

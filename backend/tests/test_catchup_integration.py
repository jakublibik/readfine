"""Integration test: catch-up/briefing must exclude retention-trimmed stubs (#18).

Runs against the real (dev) database inside a rolled-back transaction. Skips
automatically if the database is unreachable.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.services.catchup_service import (
    annotate_sources,
    build_articles_meta,
    count_catchup_articles,
    fetch_catchup_articles,
    fold_stories,
)

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _setup(session):
    u = uuid.uuid4().hex[:12]
    user = User(email=f"catchup_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="Feed", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return user, feed


async def _article(session, feed, *, title, trimmed_at=None):
    u = uuid.uuid4().hex
    a = Article(
        feed_id=feed.id, guid=u, guid_hash=u, title=title,
        content="<p>body</p>", readable_status="success",
        published_at=NOW - timedelta(hours=1), fetched_at=NOW - timedelta(hours=1),
        trimmed_at=trimmed_at,
    )
    session.add(a)
    await session.flush()
    return a


async def test_trimmed_articles_excluded_from_catchup(pg):
    user, feed = await _setup(pg)
    normal = await _article(pg, feed, title="Normal")
    await _article(pg, feed, title="Trimmed", trimmed_at=NOW - timedelta(minutes=30))

    results = await fetch_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg,
        period="7days", scope_include=None,
        filter_status="all", label_filter=None, filter_score_min=None,
    )

    titles = {r.title for r in results}
    ids = {r.id for r in results}
    assert normal.id in ids
    assert "Trimmed" not in titles


async def _label(session, user, name):
    lbl = Label(user_id=user.id, name=name)
    session.add(lbl)
    await session.flush()
    return lbl


async def _assign_label(session, user, article, label):
    session.add(ArticleLabel(user_id=user.id, article_id=article.id, label_id=label.id))
    await session.flush()


async def test_label_filter_any(pg):
    user, feed = await _setup(pg)
    labeled = await _article(pg, feed, title="Labeled")
    await _article(pg, feed, title="Unlabeled")
    lbl = await _label(pg, user, "News")
    await _assign_label(pg, user, labeled, lbl)

    results = await fetch_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg,
        period="7days", scope_include=None,
        filter_status="all", label_filter='["any"]', filter_score_min=None,
    )

    titles = {r.title for r in results}
    assert "Labeled" in titles
    assert "Unlabeled" not in titles


async def test_label_filter_specific(pg):
    user, feed = await _setup(pg)
    a_news = await _article(pg, feed, title="News article")
    a_tech = await _article(pg, feed, title="Tech article")
    news = await _label(pg, user, "News")
    tech = await _label(pg, user, "Tech")
    await _assign_label(pg, user, a_news, news)
    await _assign_label(pg, user, a_tech, tech)

    results = await fetch_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg,
        period="7days", scope_include=None,
        filter_status="all", label_filter=f'["label:{news.id}"]', filter_score_min=None,
    )

    titles = {r.title for r in results}
    assert titles == {"News article"}


async def test_label_filter_combined_with_score(pg):
    user, feed = await _setup(pg)
    lbl = await _label(pg, user, "News")

    high = await _article(pg, feed, title="High score")
    low = await _article(pg, feed, title="Low score")
    await _assign_label(pg, user, high, lbl)
    await _assign_label(pg, user, low, lbl)
    pg.add(UserArticleState(user_id=user.id, article_id=high.id, ai_score=0.9))
    pg.add(UserArticleState(user_id=user.id, article_id=low.id, ai_score=0.2))
    await pg.flush()

    results = await fetch_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg,
        period="7days", scope_include=None,
        filter_status="all", label_filter='["any"]', filter_score_min=0.5,
    )

    titles = {r.title for r in results}
    assert titles == {"High score"}


async def test_count_matches_fetch_for_every_filter(pg):
    """The UI's article count and cost estimate come from count_catchup_articles,
    the digest from fetch_catchup_articles. If the two queries drift apart, the
    estimate silently describes a different set than the one that gets sent."""
    user, feed = await _setup(pg)
    lbl = await _label(pg, user, "News")

    labeled = await _article(pg, feed, title="Labeled")
    plain = await _article(pg, feed, title="Plain")
    await _article(pg, feed, title="Trimmed", trimmed_at=NOW - timedelta(minutes=30))
    await _assign_label(pg, user, labeled, lbl)
    pg.add(UserArticleState(user_id=user.id, article_id=labeled.id, ai_score=0.9))
    pg.add(UserArticleState(
        user_id=user.id, article_id=plain.id, ai_score=0.2, dwell_seconds=60,
    ))
    await pg.flush()

    # (filters, how many of the three articles they select over the 7 day window)
    cases = [
        ({"filter_status": "all", "label_filter": None, "filter_score_min": None}, 2),
        ({"filter_status": "not_opened", "label_filter": None, "filter_score_min": None}, 1),
        ({"filter_status": "all", "label_filter": '["any"]', "filter_score_min": None}, 1),
        ({"filter_status": "all", "label_filter": f'["label:{lbl.id}"]', "filter_score_min": None}, 1),
        ({"filter_status": "all", "label_filter": None, "filter_score_min": 0.5}, 1),
        ({"filter_status": "not_opened", "label_filter": '["any"]', "filter_score_min": 0.5}, 1),
    ]
    scope = json.dumps([f"feed:{feed.id}"])
    for case, expected in cases:
        for period in ("today", "yesterday", "7days"):
            common = dict(
                user_id=user.id, tz_str="UTC", db=pg,
                period=period, scope_include=scope, **case,
            )
            fetched = await fetch_catchup_articles(**common)
            counted = await count_catchup_articles(**common)
            assert counted == len(fetched), f"{period} {case}"
            # Pin the 7 day window to real numbers, so the equality above can't
            # pass by both queries selecting nothing. The shorter periods depend
            # on the wall clock, so only the equality is checked there.
            if period == "7days":
                assert counted == expected, case


# ── story grouping in a digest ────────────────────────────────────────────────

async def _second_feed(session, user):
    """A second subscribed feed: grouping only ever pairs articles across feeds."""
    u = uuid.uuid4().hex[:12]
    feed = Feed(feed_url=f"https://ex2.invalid/{u}.xml", title="Feed 2", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return feed


async def test_collapsed_count_matches_what_folding_leaves(pg):
    """The estimate above the form and the prompt have to be the same number.

    One is COUNT(DISTINCT coalesce(story_id, -id)) in SQL, the other is fold_stories in
    Python, and nothing but this test stops them drifting apart.
    """
    user, feed = await _setup(pg)
    other = await _second_feed(pg, user)

    anchor = await _article(pg, feed, title="Outlet one on the story")
    second = await _article(pg, other, title="Outlet two on the story")
    third = await _article(pg, other, title="Outlet three on the story")
    for a in (second, third):
        a.story_id = anchor.id
    anchor.story_id = anchor.id
    await _article(pg, feed, title="Unrelated")
    await pg.flush()

    common = dict(
        user_id=user.id, tz_str="UTC", db=pg, period="7days", scope_include=None,
        filter_status="all", label_filter=None, filter_score_min=None,
    )
    fetched = await fetch_catchup_articles(**common)
    folded = fold_stories(fetched)
    counted = await count_catchup_articles(**common, collapsing=True)

    assert len(fetched) == 4
    assert counted == len(folded) == 2
    assert sum(a.folded_count for a in folded) == 2


async def test_uncollapsed_count_still_counts_articles(pg):
    """With the feature off the number has to keep meaning what it did."""
    user, feed = await _setup(pg)
    other = await _second_feed(pg, user)
    anchor = await _article(pg, feed, title="Outlet one")
    second = await _article(pg, other, title="Outlet two")
    anchor.story_id = second.story_id = anchor.id
    await pg.flush()

    counted = await count_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg, period="7days", scope_include=None,
        filter_status="all", label_filter=None, filter_score_min=None,
        collapsing=False,
    )
    assert counted == 2


async def test_kept_out_articles_leave_and_come_back_with_the_setting(pg):
    """Suppression hides an article from the list; a digest must not hand it back.

    It is read with no time spent on it, so every other filter here lets it through,
    including "not opened". Switching the setting back brings it in again, because
    hidden_at is never cleared and the current setting is what decides.
    """
    user, feed = await _setup(pg)
    hidden = await _article(pg, feed, title="Kept out")
    await _article(pg, feed, title="Ordinary")
    pg.add(UserArticleState(
        user_id=user.id, article_id=hidden.id, is_read=True,
        read_at=NOW, suppressed_at=NOW, suppressed_by="similar", hidden_at=NOW,
    ))
    await pg.flush()

    common = dict(
        user_id=user.id, tz_str="UTC", db=pg, period="7days", scope_include=None,
        filter_status="all", label_filter=None, filter_score_min=None,
    )
    with_hidden = await fetch_catchup_articles(**common)
    without = await fetch_catchup_articles(**common, exclude_hidden=True)

    assert {a.title for a in with_hidden} == {"Kept out", "Ordinary"}
    assert {a.title for a in without} == {"Ordinary"}
    assert await count_catchup_articles(**common, exclude_hidden=True) == 1


async def test_source_count_spans_the_whole_group_but_stops_at_the_access_gate(pg):
    """The marker counts coverage the reader could open, not the digest's own scope.

    A briefing narrowed to one feed folds nothing, and the number is the whole point
    there. What it must never count is a member from a feed this reader does not take:
    the grouping is global and routinely reaches into other people's subscriptions.
    """
    user, feed = await _setup(pg)
    other = await _second_feed(pg, user)

    mine = await _article(pg, feed, title="In my scope")
    theirs = await _article(pg, other, title="Other feed I take")

    stranger_feed = Feed(feed_url=f"https://ex3.invalid/{uuid.uuid4().hex[:8]}.xml",
                         title="Not subscribed", subscriber_count=1)
    pg.add(stranger_feed)
    await pg.flush()
    stranger = await _article(pg, stranger_feed, title="Someone else's feed")

    mine.story_id = theirs.story_id = stranger.story_id = mine.id
    await pg.flush()

    scope = json.dumps([f"feed:{feed.id}"])
    fetched = await fetch_catchup_articles(
        user_id=user.id, tz_str="UTC", db=pg, period="7days", scope_include=scope,
        filter_status="all", label_filter=None, filter_score_min=None,
    )
    assert [a.title for a in fetched] == ["In my scope"]

    await annotate_sources(fetched, user.id, pg)
    # The one other member behind the access gate, not the stranger's.
    assert fetched[0].source_count == 1
    assert fetched[0].folded_count == 0

    meta = build_articles_meta(fetched, include_snippet=False)
    assert meta[0]["sources"] == 1

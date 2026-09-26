"""Score condition and score sort in search.

`list_articles(score_source=..., score_op=..., score_val=..., sort_order="score")`
powers the Score row of the search modal. The source picks the scorer: "ai",
"basic" (lexical) or "relevance" (AI, else basic, what the list shows). An article
with no score from the chosen scorer never matches a condition and sorts last.

Runs against the real (dev) database inside a transaction that is always rolled
back. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.schemas.article import ArticleListItem
from app.routers.web.app.articles import _build_more_qs, search_score, story_scope
from app.services.article import list_articles

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
    """Four matching articles, newest first by age:
    a: AI 80, basic 20 | b: basic 75 only | c: no score | d: AI 30, basic 90.
    Stored 0..1, searched on the 0..100 scale the list shows."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"score_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()

    token = "zscoretok" + uuid.uuid4().hex[:10]
    arts = {}
    for i, (name, ai, lex) in enumerate([
        ("a", 0.80, 0.20), ("b", None, 0.75), ("c", None, None), ("d", 0.30, 0.90),
    ]):
        g = uuid.uuid4().hex
        when = NOW - timedelta(hours=i + 1)
        a = Article(
            feed_id=feed.id, guid=g, guid_hash=g, title=f"{token} {name}",
            content="<p>body</p>", readable_status="pending",
            published_at=when, fetched_at=when,
        )
        session.add(a)
        await session.flush()
        session.add(UserArticleState(
            user_id=user.id, article_id=a.id, ai_score=ai, lexical_score=lex,
        ))
        arts[name] = a
    await session.flush()
    return user, feed, token, arts


def _names(items, arts):
    by_id = {a.id: n for n, a in arts.items()}
    return [by_id[i.id] for i in items]


async def test_relevance_at_least_uses_ai_else_basic(pg):
    user, feed, token, arts = await _setup(pg)
    items = await list_articles(user=user, db=pg, q=token, sort_order="newest",
                                score_source="relevance", score_op="gte", score_val=70)
    # d has basic 90, but its AI 30 is the number that counts.
    assert _names(items, arts) == ["a", "b"]


async def test_basic_ignores_ai(pg):
    user, feed, token, arts = await _setup(pg)
    items = await list_articles(user=user, db=pg, q=token, sort_order="newest",
                                score_source="basic", score_op="gte", score_val=70)
    assert _names(items, arts) == ["b", "d"]


async def test_below_never_matches_a_missing_score(pg):
    user, feed, token, arts = await _setup(pg)
    items = await list_articles(user=user, db=pg, q=token, sort_order="newest",
                                score_source="ai", score_op="lt", score_val=50)
    assert _names(items, arts) == ["d"]


async def test_sort_by_score_without_query_puts_unscored_last(pg):
    user, feed, token, arts = await _setup(pg)
    items = await list_articles(user=user, db=pg, feed_id=feed.id,
                                sort_order="score", score_source="relevance")
    assert _names(items, arts) == ["a", "b", "d", "c"]


async def test_sort_by_score_with_query_and_offset(pg):
    # Offset still works for the REST API.
    user, feed, token, arts = await _setup(pg)
    first = await list_articles(user=user, db=pg, q=token, sort_order="score",
                                score_source="basic", limit=2)
    rest = await list_articles(user=user, db=pg, q=token, sort_order="score",
                               score_source="basic", limit=2, offset=2)
    assert _names(first, arts) + _names(rest, arts) == ["d", "b", "a", "c"]


def _next(item):
    return {"cursor_ts": item.sort_ts, "cursor_id": item.id, "cursor_key": item.sort_key}


async def _walk(pg, user, **kw):
    """Page one row at a time by keyset, as the infinite scroll does."""
    seen, cursor = [], {}
    for _ in range(10):
        page = await list_articles(user=user, db=pg, limit=1, **kw, **cursor)
        if not page:
            return seen
        seen += page
        cursor = _next(page[-1])
    raise AssertionError("did not end")


async def test_score_sort_keyset_walks_through_unscored(pg):
    # c has no score: the cursor has to cross from scored rows to NULL ones.
    user, feed, token, arts = await _setup(pg)
    for kw in ({"q": token}, {"feed_id": feed.id}):
        seen = await _walk(pg, user, sort_order="score", score_source="relevance", **kw)
        assert _names(seen, arts) == ["a", "b", "d", "c"]


async def test_score_sort_keyset_survives_mark_read(pg):
    # The bug offset had: rows read on scroll leave an unread list, and the next
    # page skipped as many rows as left.
    user, feed, token, arts = await _setup(pg)
    kw = dict(q=token, sort_order="score", score_source="basic", read_status="unread", limit=2)
    first = await list_articles(user=user, db=pg, **kw)
    assert _names(first, arts) == ["d", "b"]
    for item in first:
        (await pg.get(UserArticleState, (user.id, item.id))).is_read = True
    await pg.flush()
    rest = await list_articles(user=user, db=pg, **kw, **_next(first[-1]))
    assert _names(rest, arts) == ["a", "c"]


async def test_relevance_sort_keyset(pg):
    # Every row matches the token once, so the rank ties and the date decides; the
    # cursor must page through the ties without repeating or skipping.
    user, feed, token, arts = await _setup(pg)
    seen = await _walk(pg, user, q=token, sort_order="relevance")
    assert sorted(_names(seen, arts)) == ["a", "b", "c", "d"]
    assert len(seen) == 4
    assert all(i.sort_key is not None for i in seen)


async def test_threshold_matches_the_rounded_number_shown(pg):
    user, feed, token, arts = await _setup(pg)
    state = await pg.get(UserArticleState, (user.id, arts["b"].id))
    state.lexical_score = 0.696  # the list shows 70
    await pg.flush()
    at_least = await list_articles(user=user, db=pg, q=token, sort_order="newest",
                                   score_source="basic", score_op="gte", score_val=70)
    below = await list_articles(user=user, db=pg, q=token, sort_order="newest",
                                score_source="basic", score_op="lt", score_val=70)
    assert "b" in _names(at_least, arts)
    assert "b" not in _names(below, arts)


def test_search_score_normalizes():
    assert search_score(None, None, None) == {}
    # An operator without a value is no condition.
    assert search_score("ai", "gte", None) == {}
    assert search_score("bogus", "lt", 40) == {
        "score_source": "relevance", "score_op": "lt", "score_val": 40}
    # Sorting alone carries only the source.
    assert search_score("basic", "any", None, sort="score") == {"score_source": "basic"}


def test_story_scope_takes_the_condition_not_the_sort():
    cond = search_score("ai", "gte", 70)
    assert story_scope(score=cond) == cond
    assert story_scope(score=search_score("ai", None, None, sort="score")) == {}


def _item(**kw):
    return ArticleListItem(
        id=7, feed_id=1, feed_title="f", url="u", title="t", author=None,
        summary=None, snippet=None, published_at=NOW, formatted_date="x",
        estimated_read_min=None, image_url=None, is_read=False, is_starred=False,
        is_archived=False, sort_ts=NOW, **kw,
    )


def test_score_sort_pages_by_cursor_with_key():
    qs = parse_qs(_build_more_qs({"sort": "score", "score_source": "ai"}, [_item(sort_key=0.75)]),
                  keep_blank_values=True)
    assert qs["cursor_key"] == ["0.75"] and qs["cursor_id"] == ["7"]
    assert "offset" not in qs
    # A row without a score: an empty key.
    qs = parse_qs(_build_more_qs({"sort": "score"}, [_item()]), keep_blank_values=True)
    assert qs["cursor_key"] == [""]
    # Rank for a text search sorted by relevance; none for newest.
    assert "cursor_key" in _build_more_qs({"q": "x", "sort": "relevance"}, [_item(sort_key=0.1)])
    assert "cursor_key" not in _build_more_qs({"q": "x", "sort": "newest"}, [_item()])


def test_parse_cursor_key():
    from app.routers.web.app.articles import _parse_cursor_key
    assert _parse_cursor_key("0.5") == 0.5
    assert _parse_cursor_key("") is None
    assert _parse_cursor_key("nan") is None
    assert _parse_cursor_key("junk") is None


async def test_count_matches_the_list(pg):
    from app.services.article import count_articles
    user, feed, token, arts = await _setup(pg)
    kw = dict(q=token, score_source="relevance", score_op="gte", score_val=70)
    items = await list_articles(user=user, db=pg, sort_order="score", **kw)
    assert await count_articles(user, pg, collapsing=True, **kw) == len(items) == 2
    assert await count_articles(user, pg, collapsing=False, q=token) == 4


def _row(ai, lexical):
    from app.schemas.article import ArticleListItem
    return ArticleListItem.model_construct(ai_score=ai, lexical_score=lexical)


def test_rows_show_the_scorer_the_search_used():
    """A search on the basic score shows basic numbers, not the AI score beside them."""
    from app.routers.web.app.articles import pin_score_source
    basic, ai, either = _row(0.4, 0.72), _row(0.4, 0.72), _row(0.4, 0.72)
    pin_score_source([basic], "basic")
    pin_score_source([ai], "ai")
    pin_score_source([either], "relevance")
    assert (basic.score, basic.score_is_ai) == (0.72, False)
    assert (ai.score, ai.score_is_ai) == (0.4, True)
    assert (either.score, either.score_is_ai) == (0.4, True)


def test_a_pinned_scorer_without_a_score_shows_nothing():
    from app.routers.web.app.articles import pin_score_source
    row = _row(None, 0.3)
    pin_score_source([row], "ai")
    assert row.score is None


def test_pinned_source_stays_out_of_the_api():
    row = _row(0.4, 0.72)
    row._score_source = "basic"
    assert "_score_source" not in row.model_dump()

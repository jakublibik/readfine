"""Suggested terms: editing the list as written, the computation, and the data.

The text edits and `compute` are pure. The database tests use the same shape as
the other relevance tests (real dev database, one transaction, always rolled
back, skipped when unreachable) and check what only the query decides: which
reading counts as engagement, and how long a dismissal holds.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.relevance import RelevanceSuggestionDismissal
from app.models.user import User
from app.services import relevance_service as rs
from app.services import relevance_suggest_service as ss

NOW = datetime.now(timezone.utc)
DAY0 = NOW.date()


# ── editing the list ──────────────────────────────────────────────────────────

class TestAddTerm:
    def test_empty_list_becomes_the_term(self):
        assert ss.add_term("", "sourdough") == "sourdough"
        assert ss.add_term(None, "sourdough") == "sourdough"

    def test_comma_list_gets_a_comma(self):
        assert ss.add_term("cycling, baking", "sourdough") == "cycling, baking, sourdough"

    def test_list_of_lines_gets_a_line(self):
        assert ss.add_term("cycling\nbaking\n", "sourdough") == "cycling\nbaking\nsourdough"

    def test_trailing_separator_is_reused(self):
        assert ss.add_term("cycling,", "sourdough") == "cycling, sourdough"

    def test_a_term_already_there_leaves_the_list_alone(self):
        assert ss.add_term("Sourdough, cycling", "sourdough") == "Sourdough, cycling"


class TestRemoveTerm:
    def test_middle_keeps_the_spacing(self):
        assert ss.remove_term("a1, crypto, b1", "crypto") == "a1, b1"

    def test_first_and_last(self):
        assert ss.remove_term("crypto, a1", "crypto") == "a1"
        assert ss.remove_term("a1, crypto", "crypto") == "a1"

    def test_lines_and_mixed_separators(self):
        assert ss.remove_term("a1\ncrypto\nb1", "crypto") == "a1\nb1"
        assert ss.remove_term("a1, crypto\nb1; c1", "crypto") == "a1\nb1; c1"

    def test_every_spelling_goes_bullets_included(self):
        assert ss.remove_term("- Crypto\n- a1\n- crypto", "crypto") == "- a1"

    def test_multiword_term_and_partial_words_stay(self):
        text = "space exploration, space, exploration"
        assert ss.remove_term(text, "Space  Exploration") == "space, exploration"

    def test_empty_slots_around_the_term_collapse_to_one_separator(self):
        assert ss.remove_term("a1,, crypto,, b1", "crypto") == "a1, b1"
        assert ss.remove_term("a1, \ncrypto, b1", "crypto") == "a1\nb1"
        assert ss.remove_term("a1, b1, \n-\ncrypto", "crypto") == "a1, b1"

    def test_never_starts_or_ends_with_a_separator(self):
        assert ss.remove_term(",, crypto, a1", "crypto") == "a1"
        assert ss.remove_term("a1, crypto,", "crypto") == "a1"
        assert ss.remove_term("crypto", "crypto") == ""

    def test_separator_without_space_stays_without(self):
        assert ss.remove_term("a1,crypto,b1", "crypto") == "a1,b1"

    def test_a_stray_double_comma_elsewhere_is_left_alone(self):
        assert ss.remove_term("a1,, b1, crypto, c1", "crypto") == "a1,, b1, c1"

    def test_absent_term_changes_nothing(self):
        assert ss.remove_term("a1, b1", "crypto") == "a1, b1"


# ── the computation ───────────────────────────────────────────────────────────

def _rows(engaged: list[str], other: list[str]):
    """Every article its own story, on a day of its own (a week of them)."""
    rows = [(t, None, True) for t in engaged] + [(t, None, False) for t in other]
    return [(t, b, e, i, DAY0 + timedelta(days=i % 7)) for i, (t, b, e) in enumerate(rows)]


def _stats(rows):
    return rs.build_corpus_stats((rs.article_text(t, b) for t, b, *_ in rows), min_df=1)


# 400 articles that are not about anything the tests look for, so three
# articles are under one percent of the inflow.
FILLER = [f"weather report number{i}" for i in range(400)]


class TestCompute:
    def test_too_little_engagement_suggests_nothing(self):
        rows = _rows([f"sourdough loaf {i}" for i in range(ss.MIN_ENGAGED - 1)], FILLER)
        out = ss.compute(rows, [], _stats(rows), set())
        assert out.items == () and not out.enough
        assert out.engaged == ss.MIN_ENGAGED - 1

    def _reading(self):
        engaged = ([f"sourdough starter tips {i}" for i in range(4)]
                   + [f"misc story{i}" for i in range(8)])
        return _rows(engaged, FILLER)

    def test_suggests_a_topic_of_engaged_articles_with_headlines(self):
        rows = self._reading()
        out = ss.compute(rows, [], _stats(rows), set())
        adds = {s.term: s for s in out.adds}
        assert "sourdough" in adds
        assert len(adds["sourdough"].titles) == ss.EXAMPLE_TITLES
        assert all("sourdough" in t for t in adds["sourdough"].titles)

    def test_one_story_from_many_outlets_counts_once(self):
        engaged = ([f"sourdough starter tips {i}" for i in range(4)]
                   + [f"misc story{i}" for i in range(8)])
        rows = _rows(engaged, FILLER)
        # The four sourdough articles are one story reported four times.
        rows = [(t, b, e, 0 if "sourdough" in t else s, d) for t, b, e, s, d in rows]
        out = ss.compute(rows, [], _stats(rows), set())
        assert "sourdough" not in {s.term for s in out.adds}

    def test_one_day_of_news_is_not_an_interest(self):
        rows = self._reading()
        # Four separate stories, but all from the same day.
        rows = [(t, b, e, s, DAY0 if "sourdough" in t else d) for t, b, e, s, d in rows]
        out = ss.compute(rows, [], _stats(rows), set())
        assert "sourdough" not in {s.term for s in out.adds}

    def test_below_min_support_is_not_suggested(self):
        engaged = (["sourdough starter", "sourdough loaf"]
                   + [f"misc story{i}" for i in range(10)])
        rows = _rows(engaged, FILLER)
        assert "sourdough" not in {s.term for s in ss.compute(rows, [], _stats(rows), set()).adds}

    def test_a_word_common_in_the_inflow_is_not_suggested(self):
        rows = self._reading()
        assert "weather" not in {s.term for s in ss.compute(rows, [], _stats(rows), set()).adds}

    def test_stopwords_are_not_suggested(self):
        engaged = ([f"about sourdough {i}" for i in range(4)]
                   + [f"misc story{i}" for i in range(8)])
        rows = _rows(engaged, FILLER)
        assert "about" not in {s.term for s in ss.compute(rows, [], _stats(rows), set()).adds}

    def test_covered_by_a_term_through_its_prefix_is_not_suggested(self):
        rows = self._reading()
        # "sourdoughs" cuts to "sourdoug", which "sourdough" starts with.
        out = ss.compute(rows, ["sourdoughs"], _stats(rows), set())
        assert "sourdough" not in {s.term for s in out.adds}

    def test_dismissed_add_is_not_suggested(self):
        rows = self._reading()
        out = ss.compute(rows, [], _stats(rows), {("sourdough", ss.ADD)})
        assert "sourdough" not in {s.term for s in out.adds}

    def test_a_term_that_matches_often_and_is_never_read_is_flagged(self):
        engaged = [f"misc story{i}" for i in range(12)]
        other = [f"crypto news {i}" for i in range(ss.MIN_MATCHES)] + FILLER
        rows = _rows(engaged, other)
        out = ss.compute(rows, ["crypto", "sourdough"], _stats(rows), set())
        assert [(s.term, s.matched, s.engaged) for s in out.removes] == [
            ("crypto", ss.MIN_MATCHES, 0)]
        dismissed = ss.compute(rows, ["crypto"], _stats(rows), {("crypto", ss.REMOVE)})
        assert dismissed.removes == ()

    def test_a_term_with_few_matches_is_not_flagged(self):
        engaged = [f"misc story{i}" for i in range(12)]
        other = [f"crypto news {i}" for i in range(ss.MIN_MATCHES - 1)] + FILLER
        rows = _rows(engaged, other)
        assert ss.compute(rows, ["crypto"], _stats(rows), set()).removes == ()


class TestTermStats:
    def _rows(self):
        # 12 engaged of 424: 4 of them about sourdough, 20 unread about crypto.
        engaged = ([f"sourdough starter tips {i}" for i in range(4)]
                   + [f"misc story{i}" for i in range(8)])
        other = [f"crypto news {i}" for i in range(20)] + FILLER
        return _rows(engaged, other)

    def test_same_counts_as_the_removal_suggestion(self):
        rows = self._rows()
        out = ss.compute(rows, ["crypto"], _stats(rows), set())
        [remove] = out.removes
        [st] = out.terms
        assert (st.term, st.matched, st.engaged) == (remove.term, remove.matched, remove.engaged)

    def test_lift_is_the_terms_read_rate_over_the_base(self):
        rows = self._rows()
        out = ss.compute(rows, ["sourdough", "crypto"], _stats(rows), set())
        by_term = {st.term: st for st in out.terms}
        assert out.base == pytest.approx(12 / len(rows))
        assert by_term["sourdough"].lift is None  # 4 matches, under MIN_LIFT_MATCHES
        assert by_term["crypto"].lift == 0.0
        rows += [(f"sourdough bread {i}", None, True, 1000 + i, DAY0) for i in range(4)]
        out = ss.compute(rows, ["sourdough"], _stats(rows), set())
        [st] = out.terms
        assert (st.matched, st.engaged) == (8, 8)
        assert st.lift == pytest.approx(1 / out.base)

    def test_most_matches_first_and_a_term_with_none_listed(self):
        rows = self._rows()
        out = ss.compute(rows, ["bike commuting", "sourdough", "crypto"], _stats(rows), set())
        assert [(st.term, st.matched) for st in out.terms] == [
            ("crypto", 20), ("sourdough", 4), ("bike commuting", 0)]

    def test_counted_below_min_engaged_but_without_lift(self):
        engaged = [f"sourdough loaf {i}" for i in range(ss.MIN_ENGAGED - 1)]
        rows = _rows(engaged, FILLER)
        out = ss.compute(rows, ["sourdough"], _stats(rows), set())
        assert not out.enough and out.items == ()
        [st] = out.terms
        assert (st.matched, st.engaged, st.lift) == (ss.MIN_ENGAGED - 1, ss.MIN_ENGAGED - 1, None)

    def test_span_only_when_the_inflow_cap_cut_the_window(self, monkeypatch):
        rows = self._rows()
        assert ss.compute(rows, ["crypto"], _stats(rows), set()).span_days is None
        monkeypatch.setattr(ss, "MAX_INFLOW", len(rows))
        rows.sort(key=lambda r: r[4], reverse=True)  # newest first, as the query returns them
        assert ss.compute(rows, ["crypto"], _stats(rows), set()).span_days == 7


# ── the database ──────────────────────────────────────────────────────────────

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


async def _user_with_feed(session) -> tuple[User, Feed]:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"sugg_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    await session.flush()
    return user, feed


@pytest.mark.asyncio
class TestRows:
    async def test_engagement_is_dwell_link_or_star_never_is_read(self, pg):
        user, feed = await _user_with_feed(pg)
        kinds = {
            "read only": dict(is_read=True, dwell_seconds=5),
            "dwell": dict(dwell_seconds=30),
            "link": dict(link_opened=True),
            "star": dict(ever_starred=True),
            "no state": None,
        }
        for title, state in kinds.items():
            u = uuid.uuid4().hex
            a = Article(feed_id=feed.id, guid=u, guid_hash=u, title=title,
                        content="<p>x</p>", fetched_at=NOW)
            pg.add(a)
            await pg.flush()
            if state is not None:
                pg.add(UserArticleState(user_id=user.id, article_id=a.id, **state))
        await pg.flush()

        rows = await ss._rows(user.id, pg, NOW + timedelta(seconds=1))
        assert {t: e for t, _, e, *_ in rows} == {
            "read only": False, "dwell": True, "link": True, "star": True,
            "no state": False}

    async def test_only_the_readers_feeds_and_the_window(self, pg):
        user, feed = await _user_with_feed(pg)
        _, other_feed = await _user_with_feed(pg)
        for f, title, age in ((feed, "mine", 0), (other_feed, "not mine", 0),
                              (feed, "too old", ss.WINDOW_DAYS + 1)):
            u = uuid.uuid4().hex
            pg.add(Article(feed_id=f.id, guid=u, guid_hash=u, title=title,
                           fetched_at=NOW - timedelta(days=age)))
        await pg.flush()
        rows = await ss._rows(user.id, pg, NOW + timedelta(seconds=1))
        assert [t for t, *_ in rows] == ["mine"]

    async def _ages(self, pg, ages: list[float]) -> list[float]:
        """Articles fetched that many days ago; the ages `_rows` returns."""
        user, feed = await _user_with_feed(pg)
        for age in ages:
            u = uuid.uuid4().hex
            pg.add(Article(feed_id=feed.id, guid=u, guid_hash=u, title=str(age),
                           fetched_at=NOW - timedelta(days=age)))
        await pg.flush()
        return [float(t) for t, *_ in await ss._rows(user.id, pg, NOW + timedelta(seconds=1))]

    async def test_the_newest_max_inflow_when_they_span_the_floor(self, pg, monkeypatch):
        monkeypatch.setattr(ss, "MAX_INFLOW", 3)
        assert await self._ages(pg, [1, 10, 12, 14, 20]) == [1, 10, 12]

    async def test_never_fewer_days_than_the_floor(self, pg, monkeypatch):
        monkeypatch.setattr(ss, "MAX_INFLOW", 3)
        assert await self._ages(pg, [1, 2, 3, 4, 6, 10]) == [1, 2, 3, 4, 6]

    async def test_the_whole_window_when_under_max_inflow(self, pg, monkeypatch):
        monkeypatch.setattr(ss, "MAX_INFLOW", 3)
        assert await self._ages(pg, [1, 20, ss.WINDOW_DAYS + 1]) == [1, 20]

    async def test_the_floor_has_a_ceiling(self, pg, monkeypatch):
        monkeypatch.setattr(ss, "MAX_INFLOW", 3)
        monkeypatch.setattr(ss, "HARD_MAX_INFLOW", 4)
        assert await self._ages(pg, [1, 2, 3, 4, 6, 10]) == [1, 2, 3, 4]


@pytest.mark.asyncio
class TestDismiss:
    async def test_holds_for_the_period_then_expires(self, pg):
        user, _ = await _user_with_feed(pg)
        await ss.dismiss(user.id, "Sourdough", ss.ADD, pg)
        assert await ss._dismissed(user.id, pg, NOW) == {("sourdough", ss.ADD)}

        later = NOW + timedelta(days=ss.DISMISS_DAYS + 1)
        assert await ss._dismissed(user.id, pg, later) == set()

    async def test_dismissing_again_restarts_the_clock(self, pg):
        user, _ = await _user_with_feed(pg)
        await ss.dismiss(user.id, "crypto", ss.REMOVE, pg)
        await pg.execute(update(RelevanceSuggestionDismissal)
                         .where(RelevanceSuggestionDismissal.user_id == user.id)
                         .values(dismissed_at=NOW - timedelta(days=ss.DISMISS_DAYS + 5)))
        assert await ss._dismissed(user.id, pg, NOW) == set()
        await ss.dismiss(user.id, "crypto", ss.REMOVE, pg)
        assert await ss._dismissed(user.id, pg, NOW) == {("crypto", ss.REMOVE)}

    async def test_unknown_kind_is_ignored(self, pg):
        user, _ = await _user_with_feed(pg)
        await ss.dismiss(user.id, "crypto", "bogus", pg)
        assert await ss._dismissed(user.id, pg, NOW) == set()

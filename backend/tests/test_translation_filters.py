"""Filters and the basic score over translated titles (translation plan, Phase 2).

A reader who translates a feed writes filters and relevance terms in their own
language. The fetch leaves their filters for the translated title
(``translation_filters_pending``), the batch that stores the translation rescores
the article, and ``process_translation_filters`` runs the filters once, over the
translation or, when none is coming, over the original.

Real (dev) database in one rolled-back transaction, the model replaced by a stub.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.article import ArticleAiJob, UserArticleState
from app.models.feed import UserFeed
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import ArticleLabel, Label
from app.models.user import UserSettings
from app.services import filter_service as fs
from app.services import lexical_score_service as lss
from app.services import relevance_corpus_service as rcs
from app.services import translation_service as ts
from app.services.ai_service import Completion
from tests.test_translation import _feed, _reader, _run_with, _subscribe, pg  # noqa: F401

TOPIC = "qzlorp"  # a word no real feed has, so the corpus counts only ours


class DictModel:
    """Translates from a fixed dict; a headline it does not know is left out."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.calls = 0

    async def __call__(self, items, language, client, provider, model):
        self.calls += 1
        reply = [{"id": i["id"], "title": self.mapping[i["title"]]}
                 for i in items if i["title"] in self.mapping]
        return Completion(json.dumps(reply, ensure_ascii=False), 10, 5)


@pytest.fixture(autouse=True)
def clear_cache():
    rcs.reset_cache()
    yield
    rcs.reset_cache()


async def _label_filter(pg, user, field: str, op: str, value: str,
                        conditions: list[tuple[str, str, str]] | None = None) -> Label:
    label = Label(user_id=user.id, name=f"l_{uuid.uuid4().hex[:6]}")
    pg.add(label)
    await pg.flush()
    f = Filter(user_id=user.id, name="f", position=0)
    pg.add(f)
    await pg.flush()
    for c in conditions or [(field, op, value)]:
        pg.add(FilterCondition(filter_id=f.id, field=c[0], operator=c[1], value=c[2]))
    pg.add(FilterAction(filter_id=f.id, action_type="label", action_value=str(label.id)))
    await pg.flush()
    return label


async def _labeled(pg, user, articles) -> set[int]:
    return set(await pg.scalars(select(ArticleLabel.article_id).where(
        ArticleLabel.user_id == user.id,
        ArticleLabel.article_id.in_([a.id for a in articles]))))


async def _state(pg, user, article) -> UserArticleState | None:
    return await pg.scalar(select(UserArticleState).where(
        UserArticleState.user_id == user.id, UserArticleState.article_id == article.id)
        .execution_options(populate_existing=True))


async def _fetch(pg, feed, articles) -> None:
    """What both fetch paths do after the flush, minus the scoring."""
    readers = await ts.translating_readers(feed.id, pg)
    await fs.apply_filters_to_new_articles(feed.id, articles, pg, defer=readers)
    await ts.enqueue_translation_jobs(feed.id, articles, pg, readers=readers)


async def _translate(pg, model) -> None:
    p1, p2 = _run_with(model)
    with p1, p2:
        await ts.process_pending_translations(pg)
    while await fs.process_translation_filters(pg):
        pass


# ── matching ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("op,value,expected", [
    ("contains", "Trump", True),          # only the translation has it
    ("contains", "Трамп", True),          # only the original has it
    ("contains", "Biden", False),
    ("not_contains", "Trump", False),     # in one of them: not "not contained"
    ("not_contains", "Biden", True),
    ("regex", r"^Trump\b", True),
])
def test_a_title_condition_reads_the_original_and_the_translation(op, value, expected):
    article = SimpleNamespace(title="Трамп пригрозил налогами", content="", feed_id=1)
    cond = SimpleNamespace(field="title", operator=op, value=value)
    assert fs._matches_condition(cond, article, None, None,
                                 "Trump threatened taxes") is expected


def test_not_contains_over_title_or_content_needs_all_three_clear():
    article = SimpleNamespace(title="Трамп", content="nothing here", feed_id=1)
    cond = SimpleNamespace(field="title_or_content", operator="not_contains", value="Trump")
    assert fs._matches_condition(cond, article, None, None, "Trump") is False
    assert fs._matches_condition(cond, article, None, None, "Putin") is True


# ── fetch → translation → filters ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_filters_wait_for_the_translation_and_run_over_it_once(pg):
    feed, (a, b) = await _feed(pg, ["Трамп о налогах", "Погода в Минске"])
    reader = await _reader(pg, "en")
    other = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _subscribe(pg, other, feed, translate=False)
    await _label_filter(pg, reader, "title", "contains", "Trump")
    await _label_filter(pg, other, "title", "contains", "Минск")

    with patch("app.services.ai_scoring_service.enqueue_scoring_job",
               AsyncMock(return_value=False)):
        await _fetch(pg, feed, [a, b])
        # The reader who does not translate gets their filters at fetch, as before.
        assert await _labeled(pg, other, [a, b]) == {b.id}
        assert await _labeled(pg, reader, [a, b]) == set()
        assert (await _state(pg, reader, a)).translation_filters_pending

        # Not ready while the translation job is pending and fresh.
        assert await fs.process_translation_filters(pg) == 0

        await _translate(pg, DictModel({"Трамп о налогах": "Trump on taxes",
                                        "Погода в Минске": "Weather in Minsk"}))
    assert await _labeled(pg, reader, [a, b]) == {a.id}
    assert not (await _state(pg, reader, a)).translation_filters_pending

    # Exactly once: nothing is left waiting, a later pass does nothing.
    await pg.execute(ArticleLabel.__table__.delete().where(
        ArticleLabel.user_id == reader.id))
    assert await fs.process_translation_filters(pg) == 0
    assert await _labeled(pg, reader, [a, b]) == set()


@pytest.mark.asyncio
async def test_a_reader_without_filters_is_not_marked(pg):
    feed, (a,) = await _feed(pg, ["Трамп"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _fetch(pg, feed, [a])
    state = await _state(pg, reader, a)
    assert state is None or not state.translation_filters_pending


@pytest.mark.asyncio
async def test_a_translation_that_failed_for_good_runs_the_filters_over_the_original(pg):
    feed, (a,) = await _feed(pg, ["Трамп о налогах"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _label_filter(pg, reader, "title", "contains", "Трамп")
    await _fetch(pg, feed, [a])

    job = await pg.scalar(select(ArticleAiJob).where(
        ArticleAiJob.article_id == a.id, ArticleAiJob.operation == "translation"))
    job.status = "failed"
    await pg.flush()
    assert await fs.process_translation_filters(pg) == 1
    assert await _labeled(pg, reader, [a]) == {a.id}
    assert not (await _state(pg, reader, a)).translation_filters_pending


@pytest.mark.asyncio
async def test_a_translation_that_takes_too_long_gives_way_to_the_original(pg):
    feed, (a,) = await _feed(pg, ["Трамп о налогах"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _label_filter(pg, reader, "title", "contains", "Трамп")
    await _fetch(pg, feed, [a])

    state = await _state(pg, reader, a)
    state.created_at = datetime.now(timezone.utc) - fs.TRANSLATION_FILTERS_WAIT - timedelta(minutes=1)
    await pg.flush()
    assert await fs.process_translation_filters(pg) == 1
    assert await _labeled(pg, reader, [a]) == {a.id}


@pytest.mark.asyncio
async def test_with_ai_off_the_filters_do_not_wait(pg):
    from app.models.settings import AppSettings
    feed, (a,) = await _feed(pg, ["Трамп о налогах"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _label_filter(pg, reader, "title", "contains", "Трамп")
    await _fetch(pg, feed, [a])

    (await pg.get(AppSettings, 1)).ai_enabled = False
    await pg.flush()
    assert await fs.process_translation_filters(pg) == 1
    assert await _labeled(pg, reader, [a]) == {a.id}


@pytest.mark.asyncio
async def test_a_label_from_the_deferred_pass_parks_the_relevance_filters(pg):
    feed, (a,) = await _feed(pg, ["Трамп о налогах"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _label_filter(pg, reader, "title", "contains", "Trump")
    await _label_filter(pg, reader, "relevance_score", "gte", "50")
    # Scored straight from the label, not after a readable extraction.
    uf = await pg.scalar(select(UserFeed).where(UserFeed.user_id == reader.id))
    uf.extract_readable = False
    await pg.flush()
    await _fetch(pg, feed, [a])

    with patch("app.services.ai_scoring_service.enqueue_scoring_job",
               AsyncMock(return_value=True)) as enqueue:
        await _translate(pg, DictModel({"Трамп о налогах": "Trump on taxes"}))
    enqueue.assert_awaited_once()
    state = await _state(pg, reader, a)
    assert not state.translation_filters_pending
    assert state.relevance_filters_pending


@pytest.mark.asyncio
async def test_the_preview_matches_the_translation(pg):
    feed, (a, b) = await _feed(pg, ["Трамп о налогах", "Погода"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _fetch(pg, feed, [a, b])
    await _translate(pg, DictModel({"Трамп о налогах": "Trump on taxes", "Погода": "Weather"}))

    await _label_filter(pg, reader, "title", "contains", "Trump")
    f = await pg.scalar(select(Filter).where(Filter.user_id == reader.id))
    result = await fs.test_filter(reader.id, f.id, pg)
    assert result.matched_count == 1
    assert [s.title for s in result.samples] == ["Trump on taxes"]

    # Translation turned off for the feed: the list shows the original again,
    # and so does the matching.
    uf = await pg.scalar(select(UserFeed).where(UserFeed.user_id == reader.id))
    uf.translate = False
    await pg.flush()
    assert (await fs.test_filter(reader.id, f.id, pg)).matched_count == 0


# ── basic score ───────────────────────────────────────────────────────────────

async def _with_terms(pg, user) -> None:
    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    s.relevance_terms = TOPIC
    s.basic_scoring_enabled = True
    await pg.flush()
    await _feed(pg, [f"{TOPIC} background {i}" for i in range(3)])
    await rcs.rebuild(pg, window_days=1, min_df=3)


@pytest.mark.asyncio
async def test_the_basic_score_follows_the_translation_and_survives_a_rebuild(pg):
    feed, (a,) = await _feed(pg, ["Новости о кзлорпе"])
    reader = await _reader(pg, "en")
    stranger = await _reader(pg, "en")  # same feed, not translating
    await _subscribe(pg, reader, feed)
    await _subscribe(pg, stranger, feed, translate=False)
    await _with_terms(pg, reader)
    await _with_terms(pg, stranger)

    await lss.score_new_articles(pg, feed.id, [a])
    assert (await _state(pg, reader, a)).lexical_score == 0.0

    await _fetch(pg, feed, [a])
    await _translate(pg, DictModel({"Новости о кзлорпе": f"News about {TOPIC}"}))
    translated = (await _state(pg, reader, a)).lexical_score
    assert translated > 0.0
    assert (await _state(pg, stranger, a)).lexical_score == 0.0

    # The backfill after a corpus rebuild reads the same text, so the score holds.
    terms = (await lss.load_terms(pg, [reader.id]))[reader.id]
    stats = await lss.stats_for(pg, {reader.id: terms})
    await lss.backfill_user(pg, reader.id, terms, stats)
    assert (await _state(pg, reader, a)).lexical_score == translated


@pytest.mark.asyncio
async def test_the_suggestions_read_what_the_score_reads(pg):
    from app.services import relevance_suggest_service as rss

    feed, (a, b) = await _feed(pg, ["Трамп о налогах", "Без перевода"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _fetch(pg, feed, [a, b])
    await _translate(pg, DictModel({"Трамп о налогах": "Trump on taxes"}))

    rows = await rss._rows(reader.id, pg, datetime.now(timezone.utc))
    texts = {(title, body) for title, body, *_ in rows}
    assert ("Trump on taxes", None) in texts      # no body in the feed's language
    assert ("Без перевода", "b") in texts          # untranslated: as before


# ── automatic summary ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_short_article_of_a_translated_feed_is_still_summarized(pg):
    from app.services.ai_summary_service import enqueue_summary_job

    reader = await _reader(pg, "en")
    translated, (a,) = await _feed(pg, ["Трамп"])
    plain, (b,) = await _feed(pg, ["Trump"])
    await _subscribe(pg, reader, translated)
    await _subscribe(pg, reader, plain, translate=False)
    body = "<p>" + "слово " * 120 + "</p>"  # ~720 characters: over the floor, under 1,700
    a.content = body
    b.content = body
    await pg.flush()

    assert await enqueue_summary_job(a, reader.id, pg)
    assert not await enqueue_summary_job(b, reader.id, pg)  # the reader's minimum holds

    a.content = "<p>коротко</p>"  # a bare headline is still nothing to summarize
    await pg.flush()
    await pg.execute(ArticleAiJob.__table__.delete().where(ArticleAiJob.article_id == a.id))
    assert not await enqueue_summary_job(a, reader.id, pg)


@pytest.mark.asyncio
async def test_a_rescoring_error_does_not_cost_the_translation(pg):
    feed, (a,) = await _feed(pg, ["Трамп"])
    reader = await _reader(pg, "en")
    await _subscribe(pg, reader, feed)
    await _fetch(pg, feed, [a])

    with patch.object(lss, "rescore_articles", AsyncMock(side_effect=RuntimeError("boom"))):
        await _translate(pg, DictModel({"Трамп": "Trump"}))
    job = await pg.scalar(select(ArticleAiJob).where(
        ArticleAiJob.article_id == a.id, ArticleAiJob.operation == "translation"))
    assert job.status == "success"
    assert await ts.reader_titles(pg, [(reader.id, a.id)]) == {(reader.id, a.id): "Trump"}

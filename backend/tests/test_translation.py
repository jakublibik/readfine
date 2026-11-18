"""AI title translation: parsing the model's reply, queueing jobs at fetch, and the
batch runner (partial answers, halving, retries, isolation between readers).

The database tests use the real (dev) database in one transaction, always rolled
back, and are skipped when it is unreachable. The model is replaced by a stub that
answers from a dict, so each test decides what comes back for which headline.
"""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import AiUsageLog, Article, ArticleAiJob, ArticleTranslation
from app.models.feed import Feed, UserFeed
from app.models.settings import AppSettings
from app.models.user import User, UserSettings
from app.services import translation_service as ts
from app.services.ai_service import Completion, ProviderEmptyResponse


# ── parsing ───────────────────────────────────────────────────────────────────

ORIG = {1: "Первый заголовок", 2: "Второй заголовок"}


def test_parses_a_plain_array():
    raw = '[{"id": 1, "title": "First headline"}, {"id": 2, "title": "Second headline"}]'
    assert ts.parse_translations(raw, ORIG) == {1: "First headline", 2: "Second headline"}


def test_tolerates_a_code_fence_and_string_ids():
    raw = '```json\n[{"id": "1", "title": "First  headline\\n"}]\n```'
    assert ts.parse_translations(raw, ORIG) == {1: "First headline"}


def test_drops_ids_that_were_not_asked_for():
    raw = '[{"id": 1, "title": "First"}, {"id": 99, "title": "Stray"}]'
    assert ts.parse_translations(raw, ORIG) == {1: "First"}


def test_drops_empty_and_overlong_answers():
    essay = "This headline is about " + "x" * 400
    raw = json.dumps([{"id": 1, "title": "  "}, {"id": 2, "title": essay}])
    assert ts.parse_translations(raw, ORIG) == {}


@pytest.mark.parametrize("raw", ["", "Sorry, I can't help", "[{]", '{"id": 1}', "[1, 2]"])
def test_unusable_reply_gives_nothing(raw):
    assert ts.parse_translations(raw, ORIG) == {}


def test_every_language_name_is_prompt_safe():
    # The name goes into the prompt as is; nothing in the list may carry markup or
    # a line break.
    for code, name in ts.LANGUAGES.items():
        assert code.isalpha() and code.islower() and len(code) <= 10
        assert "\n" not in name and "{" not in name


# ── database ──────────────────────────────────────────────────────────────────

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
        app = await session.get(AppSettings, 1)
        if app is None:
            app = AppSettings(id=1)
            session.add(app)
        app.ai_enabled = True
        await session.flush()
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _reader(pg, language: str | None = "en", slot: str = "fast") -> User:
    u = uuid.uuid4().hex[:12]
    user = User(email=f"tr_{u}@test.invalid", password_hash="x", display_name="t")
    pg.add(user)
    await pg.flush()
    pg.add(UserSettings(
        user_id=user.id, ai_fast_provider="anthropic", ai_fast_model="claude-haiku-5-5",
        ai_quality_provider="anthropic", ai_quality_model="claude-sonnet-5-5",
        ai_translation_language=language, ai_translation_slot=slot,
    ))
    await pg.flush()
    return user


async def _feed(pg, titles: list[str]) -> tuple[Feed, list[Article]]:
    u = uuid.uuid4().hex[:12]
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    pg.add(feed)
    await pg.flush()
    articles = []
    for t in titles:
        g = uuid.uuid4().hex
        a = Article(feed_id=feed.id, guid=g, guid_hash=g, title=t, content="b",
                    fetched_at=datetime.now(timezone.utc))
        pg.add(a)
        articles.append(a)
    await pg.flush()
    return feed, articles


async def _subscribe(pg, user: User, feed: Feed, translate: bool = True) -> None:
    pg.add(UserFeed(user_id=user.id, feed_id=feed.id, translate=translate))
    await pg.flush()


class FakeModel:
    """Stands in for translate_titles. Translates by prefixing the language, except
    for headlines in *refuse* (left out of the reply) and, when *empty_on* is in the
    batch, answers nothing at all, the way a refusal looks from a provider."""

    def __init__(self, refuse=(), empty_on=(), error: Exception | None = None,
                 truncate_over: int | None = None):
        self.refuse = set(refuse)
        self.empty_on = set(empty_on)
        self.error = error
        self.truncate_over = truncate_over
        self.calls: list[list[str]] = []

    async def __call__(self, items, language, client, provider, model):
        self.calls.append([i["title"] for i in items])
        if self.error is not None:
            raise self.error
        if any(i["title"] in self.empty_on for i in items):
            raise ProviderEmptyResponse("empty")
        if self.truncate_over is not None and len(items) > self.truncate_over:
            return Completion('[{"id": 1, "ti', 10, 500, truncated=True)
        reply = [{"id": i["id"], "title": f"{language}: {i['title']}"}
                 for i in items if i["title"] not in self.refuse]
        return Completion(json.dumps(reply, ensure_ascii=False), 10 * len(items), 5 * len(items))


def _run_with(model: FakeModel):
    """Patch the model call and the client factory for a process run."""
    return (
        patch("app.services.ai_service.translate_titles", new=model),
        patch("app.services.ai_service.get_ai_client",
              new=AsyncMock(return_value=(MagicMock(), "anthropic", "claude-haiku-5-5"))),
    )


async def _process(pg, model: FakeModel) -> int:
    p1, p2 = _run_with(model)
    with p1, p2:
        return await ts.process_pending_translations(pg)


async def _jobs(pg, articles) -> dict[int, ArticleAiJob]:
    rows = (await pg.scalars(select(ArticleAiJob).where(
        ArticleAiJob.article_id.in_([a.id for a in articles]),
        ArticleAiJob.operation == "translation",
    ))).all()
    return {j.article_id: j for j in rows}


async def _translations(pg, user: User) -> dict[int, str]:
    rows = (await pg.scalars(select(ArticleTranslation).where(
        ArticleTranslation.user_id == user.id))).all()
    return {t.article_id: t.title for t in rows}


@pytest.mark.asyncio
async def test_enqueue_only_for_readers_who_translate_the_feed(pg):
    feed, articles = await _feed(pg, ["А", "Б"])
    on = await _reader(pg, "en")
    off = await _reader(pg, "en")
    no_lang = await _reader(pg, None)
    await _subscribe(pg, on, feed)
    await _subscribe(pg, off, feed, translate=False)
    await _subscribe(pg, no_lang, feed)

    assert await ts.enqueue_translation_jobs(feed.id, articles, pg) == 2
    assert pg.info.pop(ts.ENQUEUED_FLAG) is True
    jobs = (await pg.scalars(select(ArticleAiJob).where(
        ArticleAiJob.article_id.in_([a.id for a in articles])))).all()
    assert {j.user_id for j in jobs} == {on.id}

    # Idempotent: a second pass over the same articles queues nothing.
    assert await ts.enqueue_translation_jobs(feed.id, articles, pg) == 0
    assert ts.ENQUEUED_FLAG not in pg.info


@pytest.mark.asyncio
async def test_enqueue_respects_the_ai_kill_switch(pg):
    (await pg.get(AppSettings, 1)).ai_enabled = False
    feed, articles = await _feed(pg, ["А"])
    await _subscribe(pg, await _reader(pg), feed)
    assert await ts.enqueue_translation_jobs(feed.id, articles, pg) == 0


@pytest.mark.asyncio
async def test_batch_is_translated_stored_and_logged(pg):
    feed, articles = await _feed(pg, ["Один", "Два", "Три"])
    user = await _reader(pg, "cs")
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    model = FakeModel()
    assert await _process(pg, model) == 3
    assert len(model.calls) == 1
    assert await _translations(pg, user) == {
        a.id: f"Czech: {a.title}" for a in articles}
    jobs = await _jobs(pg, articles)
    assert {j.status for j in jobs.values()} == {"success"}
    assert {j.model for j in jobs.values()} == {"claude-haiku-5-5"}

    logs = (await pg.scalars(select(AiUsageLog).where(
        AiUsageLog.user_id == user.id, AiUsageLog.operation == "translation"))).all()
    assert [(l.model_slot, l.input_tokens, l.output_tokens) for l in logs] == [("fast", 30, 15)]

    from app.services.stats_service import get_ai_cost_stats
    stats = await get_ai_cost_stats(user.id, pg)
    row = next(r for r in stats.operations if r.operation == "translation")
    assert (row.slot, row.count, row.input_tokens, row.output_tokens) == ("fast", 1, 30, 15)
    assert stats.operations[1] is row  # beside scoring, the other fast-slot row


@pytest.mark.asyncio
async def test_partial_reply_keeps_the_good_ones_and_retries_the_rest_smaller(pg):
    titles = ["Один", "Два", "Три", "Четыре"]
    feed, articles = await _feed(pg, titles)
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    model = FakeModel(refuse={"Три"})
    await _process(pg, model)

    got = await _translations(pg, user)
    assert set(got) == {a.id for a in articles if a.title != "Три"}
    # Whole batch, then the missing one alone.
    assert model.calls == [titles, ["Три"]]
    jobs = await _jobs(pg, articles)
    stuck = jobs[articles[2].id]
    assert stuck.status == "pending" and stuck.retry_count == 1
    assert stuck.next_retry_at is not None


@pytest.mark.asyncio
async def test_an_empty_answer_is_halved_down_to_the_headline_behind_it(pg):
    titles = ["Один", "Два", "Три", "Четыре"]
    feed, articles = await _feed(pg, titles)
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    model = FakeModel(empty_on={"Четыре"})
    await _process(pg, model)

    assert set(await _translations(pg, user)) == {a.id for a in articles[:3]}
    assert model.calls == [titles, ["Один", "Два"], ["Три", "Четыре"], ["Три"], ["Четыре"]]
    jobs = await _jobs(pg, articles)
    assert jobs[articles[3].id].retry_count == 1
    assert jobs[articles[3].id].status == "pending"
    # One headline the model will not do is not the reader's key or provider.
    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    assert s.last_ai_error is None


@pytest.mark.asyncio
async def test_a_cut_off_reply_is_split(pg):
    titles = ["Один", "Два", "Три", "Четыре"]
    feed, articles = await _feed(pg, titles)
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    model = FakeModel(truncate_over=2)
    await _process(pg, model)
    assert len(await _translations(pg, user)) == 4
    assert model.calls == [titles, ["Один", "Два"], ["Три", "Четыре"]]


@pytest.mark.asyncio
async def test_a_provider_error_fails_the_batch_without_splitting(pg):
    feed, articles = await _feed(pg, ["Один", "Два", "Три"])
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    err = Exception("invalid x-api-key")
    err.status_code = 401
    model = FakeModel(error=err)
    await _process(pg, model)

    assert len(model.calls) == 1
    jobs = await _jobs(pg, articles)
    assert {j.status for j in jobs.values()} == {"failed"}
    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    assert s.last_ai_error.startswith("Translation error:")


@pytest.mark.asyncio
async def test_a_headline_that_keeps_failing_gives_up_after_the_retry_limit(pg):
    feed, articles = await _feed(pg, ["Один"])
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    model = FakeModel(refuse={"Один"})
    job = (await _jobs(pg, articles))[articles[0].id]
    for _ in range(5):
        job.next_retry_at = None  # due again now
        await pg.flush()
        await _process(pg, model)
        if job.status != "pending":
            break
    assert job.status == "failed"
    assert job.retry_count == 3
    assert len(model.calls) == 3
    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    assert s.last_ai_error is None


@pytest.mark.asyncio
async def test_readers_get_their_own_translations_in_their_own_language(pg):
    feed, articles = await _feed(pg, ["Один"])
    cs = await _reader(pg, "cs")
    de = await _reader(pg, "de")
    await _subscribe(pg, cs, feed)
    await _subscribe(pg, de, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    await _process(pg, FakeModel())

    aid = articles[0].id
    assert await _translations(pg, cs) == {aid: "Czech: Один"}
    assert await _translations(pg, de) == {aid: "German: Один"}
    rows = (await pg.scalars(select(ArticleTranslation).where(
        ArticleTranslation.article_id == aid))).all()
    assert {(r.user_id, r.language) for r in rows} == {(cs.id, "cs"), (de.id, "de")}


@pytest.mark.asyncio
async def test_jobs_are_skipped_once_the_feed_is_no_longer_translated(pg):
    feed, articles = await _feed(pg, ["Один"])
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)
    uf = await pg.scalar(select(UserFeed).where(UserFeed.user_id == user.id))
    uf.translate = False
    await pg.flush()

    model = FakeModel()
    await _process(pg, model)
    assert model.calls == []
    assert (await _jobs(pg, articles))[articles[0].id].status == "skipped"


@pytest.mark.asyncio
async def test_without_a_usable_slot_nothing_is_called(pg):
    feed, articles = await _feed(pg, ["Один"])
    user = await _reader(pg)
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    model = FakeModel()
    with patch("app.services.ai_service.translate_titles", new=model), \
         patch("app.services.ai_service.get_ai_client",
               new=AsyncMock(return_value=(None, None, None))):
        await ts.process_pending_translations(pg)
    assert model.calls == []
    assert (await _jobs(pg, articles))[articles[0].id].status == "skipped"


@pytest.mark.asyncio
async def test_main_slot_is_used_when_chosen(pg):
    feed, articles = await _feed(pg, ["Один"])
    user = await _reader(pg, slot="quality")
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    factory = AsyncMock(return_value=(MagicMock(), "anthropic", "claude-sonnet-5-5"))
    with patch("app.services.ai_service.translate_titles", new=FakeModel()), \
         patch("app.services.ai_service.get_ai_client", new=factory):
        await ts.process_pending_translations(pg)
    assert factory.await_args.args[1] == "quality"
    log = await pg.scalar(select(AiUsageLog).where(AiUsageLog.user_id == user.id))
    assert (log.model_slot, log.model) == ("quality", "claude-sonnet-5-5")


def test_kick_starts_a_run_only_after_jobs_were_queued():
    db = MagicMock()
    db.info = {}
    with patch("app.utils.background.spawn_background") as spawn:
        ts.kick_translations(db)
        spawn.assert_not_called()
        db.info[ts.ENQUEUED_FLAG] = True
        ts.kick_translations(db)
        spawn.assert_called_once()
        spawn.call_args.args[0].close()  # the coroutine, never awaited here
    assert ts.ENQUEUED_FLAG not in db.info


# ── backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_queues_the_latest_unread_visible_untranslated_articles(pg):
    from app.models.article import UserArticleState

    feed, articles = await _feed(pg, ["read", "hidden", "trimmed", "done", "old", "new1", "new2"])
    other, other_articles = await _feed(pg, ["other feed"])
    user = await _reader(pg, "cs")
    await _subscribe(pg, user, feed)
    await _subscribe(pg, user, other, translate=False)
    read, hidden, trimmed, done, old, new1, new2 = articles
    now = datetime.now(timezone.utc)
    pg.add(UserArticleState(user_id=user.id, article_id=read.id, is_read=True))
    pg.add(UserArticleState(user_id=user.id, article_id=hidden.id, hidden_at=now))
    trimmed.trimmed_at = now
    pg.add(ArticleTranslation(user_id=user.id, article_id=done.id, language="cs",
                              title="hotovo", model="m"))
    await pg.flush()

    with patch.object(ts, "BACKFILL_LIMIT", 2):
        assert await ts.backfill_translations(user.id, pg) == 2
    assert pg.info.pop(ts.ENQUEUED_FLAG) is True
    # The newest two of what is left; "old" waits for being under the limit, and a
    # feed the reader does not translate gets nothing.
    assert set(await _jobs(pg, articles + other_articles)) == {new1.id, new2.id}


@pytest.mark.asyncio
async def test_backfill_only_touches_the_feeds_it_is_given(pg):
    feed_a, a = await _feed(pg, ["А"])
    feed_b, b = await _feed(pg, ["Б"])
    user = await _reader(pg)
    await _subscribe(pg, user, feed_a)
    await _subscribe(pg, user, feed_b)

    assert await ts.backfill_translations(user.id, pg, feed_ids=[feed_b.id]) == 1
    assert set(await _jobs(pg, a + b)) == {b[0].id}
    assert await ts.backfill_translations(user.id, pg, feed_ids=[]) == 0


@pytest.mark.asyncio
async def test_backfill_needs_a_language_and_ai_on(pg):
    feed, articles = await _feed(pg, ["А"])
    user = await _reader(pg, None)
    await _subscribe(pg, user, feed)
    assert await ts.backfill_translations(user.id, pg) == 0

    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    s.ai_translation_language = "cs"
    (await pg.get(AppSettings, 1)).ai_enabled = False
    await pg.flush()
    assert await ts.backfill_translations(user.id, pg) == 0
    assert ts.ENQUEUED_FLAG not in pg.info


@pytest.mark.asyncio
async def test_a_language_change_retranslates_through_the_existing_jobs(pg):
    feed, articles = await _feed(pg, ["Один", "Два"])
    user = await _reader(pg, "cs")
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)
    await _process(pg, FakeModel(refuse={"Два"}))
    jobs = await _jobs(pg, articles)
    jobs[articles[1].id].status = "failed"  # as if out of retries
    jobs[articles[1].id].retry_count = 3
    await pg.flush()

    s = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    s.ai_translation_language = "de"
    await pg.flush()
    assert await ts.backfill_translations(user.id, pg) == 2

    for job in (await _jobs(pg, articles)).values():
        await pg.refresh(job)
        assert (job.status, job.retry_count, job.next_retry_at) == ("pending", 0, None)
    await _process(pg, FakeModel())
    rows = (await pg.scalars(select(ArticleTranslation).where(
        ArticleTranslation.user_id == user.id))).all()
    assert {(r.article_id, r.language) for r in rows} == {
        (articles[0].id, "cs"), (articles[0].id, "de"), (articles[1].id, "de")}

    # Back to Czech: only the one never done in Czech is queued again.
    s.ai_translation_language = "cs"
    await pg.flush()
    assert await ts.backfill_translations(user.id, pg) == 1


@pytest.mark.asyncio
async def test_a_language_change_during_a_call_leaves_the_jobs_for_the_new_language(pg):
    from sqlalchemy import update

    feed, articles = await _feed(pg, ["Один", "Два"])
    user = await _reader(pg, "cs")
    await _subscribe(pg, user, feed)
    await ts.enqueue_translation_jobs(feed.id, articles, pg)

    class SwitchesMidCall(FakeModel):
        async def __call__(self, items, language, *rest):
            if not self.calls:  # the reader saves German while Czech is on its way
                await pg.execute(update(UserSettings).where(UserSettings.user_id == user.id)
                                 .values(ai_translation_language="de"))
            return await super().__call__(items, language, *rest)

    model = SwitchesMidCall()
    await _process(pg, model)

    assert len(model.calls) == 2  # the Czech batch, then the same jobs in German
    rows = (await pg.scalars(select(ArticleTranslation).where(
        ArticleTranslation.user_id == user.id))).all()
    assert {(r.article_id, r.language) for r in rows} == {
        (a.id, lang) for a in articles for lang in ("cs", "de")}
    assert {j.status for j in (await _jobs(pg, articles)).values()} == {"success"}

"""A starred article's automatic summary waits for the full text.

The star queues the summary job at once, marked as waiting, and whichever path ends
the extraction makes it runnable. Real (dev) database in one rolled-back transaction,
the model and the page fetch replaced by stubs.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, update
from starlette.requests import Request

from app.models.article import ArticleAiJob, UserArticleState
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.routers.web.app import ai as ai_routes
from app.routers.web.app import articles as routes
from app.services import ai_summary_service as summaries
from app.services import readable_service as readable
from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
from app.services.ai_service import Completion
from tests.test_translation import _feed, _reader, _subscribe, pg  # noqa: F401

SHORT = "Excerpt."
MIDDLING = "word " * 160   # 800 characters: over 500, under the default 1700
FULL = "full " * 500       # 2500 characters


@pytest.fixture
def model():
    call = AsyncMock(return_value=Completion("The summary", 10, 5))
    with patch("app.services.ai_service.summarize_article", new=call), \
            patch("app.services.ai_service.get_ai_client",
                  new=AsyncMock(return_value=(MagicMock(), "anthropic", "claude-sonnet-5-5"))):
        yield call


@pytest.fixture
def spawned():
    with patch.object(routes, "spawn_background",
                      side_effect=lambda coro, name=None: coro.close()) as spawn:
        yield spawn


async def _setup(pg, *, content=SHORT, translate=False, extract=True, url="https://ex.invalid/a"):
    feed, (article,) = await _feed(pg, ["Headline"])
    article.content, article.url, article.readable_status = content, url, "skipped"
    user = await _reader(pg, "en")
    await _subscribe(pg, user, feed, translate=translate)
    uf = await pg.scalar(select(UserFeed).where(UserFeed.user_id == user.id))
    uf.extract_readable = extract
    settings = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    settings.ai_summary_enabled_default = True
    settings.ai_min_content_chars = 1700
    await pg.flush()
    return user, article


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "query_string": b"", "state": {}})


async def _star(pg, user, article):
    return await routes.htmx_toggle_star(article.id, _request(), user, pg)


async def _job(pg, user, article):
    return await pg.scalar(select(ArticleAiJob).where(
        ArticleAiJob.user_id == user.id, ArticleAiJob.article_id == article.id,
        ArticleAiJob.operation == "summary",
    ).execution_options(populate_existing=True))


async def _batch(pg, user) -> int:
    # Other accounts' jobs in the dev database are none of the test's business.
    await pg.execute(update(ArticleAiJob).where(
        ArticleAiJob.user_id != user.id, ArticleAiJob.operation == "summary",
        ArticleAiJob.status == "pending",
    ).values(status="skipped"))
    return await summaries.process_pending_summaries(pg)


async def _extracted(pg, article, content=FULL, http_status=200):
    readable.apply_readable_result(article, content, None if content else "gone", http_status)
    await run_pipeline_for_article_all_users(article, pg)


@pytest.mark.parametrize("excerpt,translate", [(SHORT, False), (MIDDLING, True), (FULL, False)])
async def test_star_waits_whatever_the_excerpt(pg, model, spawned, excerpt, translate):
    user, article = await _setup(pg, content=excerpt, translate=translate)
    response = await _star(pg, user, article)

    assert article.readable_status == "pending"
    assert (await _job(pg, user, article)).job_params == summaries.AWAITS_READABLE
    assert "summaryStarted" not in response.headers["HX-Trigger"]
    spawned.assert_not_called()
    assert await _batch(pg, user) == 0
    model.assert_not_awaited()


async def test_summary_is_written_from_the_full_text(pg, model, spawned):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    await _extracted(pg, article)

    model.assert_awaited_once()
    assert "full full" in model.await_args.args[0]
    assert SHORT not in model.await_args.args[0]
    assert (await _job(pg, user, article)).status == "success"


async def test_every_waiting_reader_gets_one(pg, model, spawned):
    user, article = await _setup(pg)
    other = await _reader(pg, "en")
    pg.add(UserFeed(user_id=other.id, feed_id=article.feed_id, extract_readable=True))
    settings = await pg.scalar(select(UserSettings).where(UserSettings.user_id == other.id))
    settings.ai_summary_enabled_default = True
    await pg.flush()
    await _star(pg, user, article)
    await _star(pg, other, article)
    await _extracted(pg, article)

    assert model.await_count == 2


async def test_full_text_still_too_short_leaves_no_job(pg, model, spawned):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    await _extracted(pg, article, MIDDLING)

    model.assert_not_awaited()
    assert await _job(pg, user, article) is None


async def test_translated_feed_needs_only_500_characters(pg, model, spawned):
    user, article = await _setup(pg, translate=True)
    await _star(pg, user, article)
    await _extracted(pg, article, MIDDLING)

    model.assert_awaited_once()


@pytest.mark.parametrize("excerpt,calls", [(FULL, 1), (SHORT, 0)])
async def test_failed_extraction_falls_back_to_the_feed(pg, model, spawned, excerpt, calls):
    user, article = await _setup(pg, content=excerpt)
    await _star(pg, user, article)
    await _extracted(pg, article, None, http_status=404)

    assert article.readable_status == "failed"
    assert model.await_count == calls


async def test_a_scheduled_retry_keeps_it_waiting(pg, model, spawned):
    user, article = await _setup(pg, content=FULL)
    await _star(pg, user, article)
    readable.apply_readable_result(article, None, "timeout", None)
    await pg.flush()

    assert await _batch(pg, user) == 0
    model.assert_not_awaited()


async def test_blocked_feed_releases_it(pg, model, spawned):
    user, article = await _setup(pg, content=FULL)
    await _star(pg, user, article)
    await readable._disable_readable_for_feed(article.feed_id, pg, pending_error="HTTP 403")

    model.assert_awaited_once()


async def test_cancelled_extraction_is_picked_up_by_the_batch(pg, model, spawned):
    # What the full-content check does inside the fetch: no pipeline, only the status.
    user, article = await _setup(pg, content=FULL)
    await _star(pg, user, article)
    article.readable_status = "skipped"
    await pg.flush()

    assert await _batch(pg, user) == 1
    model.assert_awaited_once()


async def test_unstar_cancels_the_wait(pg, model, spawned):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    await _star(pg, user, article)
    await _extracted(pg, article)

    model.assert_not_awaited()
    assert await _job(pg, user, article) is None


@pytest.mark.parametrize(
    "change", ["unstarred_over_api", "feed_switched_off", "auto_summary_switched_off"])
async def test_what_changed_during_the_wait_counts(pg, model, spawned, change):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    if change == "unstarred_over_api":
        state = await pg.scalar(select(UserArticleState).where(
            UserArticleState.user_id == user.id))
        state.is_starred = False
    elif change == "auto_summary_switched_off":
        settings = await pg.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
        settings.ai_summary_enabled_default = False
    else:
        uf = await pg.scalar(select(UserFeed).where(UserFeed.user_id == user.id))
        uf.ai_summary_enabled = False
    await pg.flush()
    await _extracted(pg, article)

    model.assert_not_awaited()
    # Gone rather than skipped, so the Summarize button starts clean.
    assert await _job(pg, user, article) is None


async def test_the_button_does_not_wait(pg, model, spawned):
    user, article = await _setup(pg, content=FULL)
    await _star(pg, user, article)
    text, _, error = await summaries.run_summary_on_demand(article, user.id, pg)

    assert (text, error) == ("The summary", None)


@pytest.mark.parametrize("extract,url", [(False, "https://ex.invalid/a"), (True, None), (True, "")])
async def test_nothing_to_wait_for_runs_at_once(pg, model, spawned, extract, url):
    user, article = await _setup(pg, content=FULL, extract=extract, url=url)
    response = await _star(pg, user, article)

    assert "summaryStarted" in response.headers["HX-Trigger"]
    spawned.assert_called_once()
    assert await _batch(pg, user) == 1


async def test_opening_the_article_releases_it(pg, model, spawned):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=pg)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("app.database.async_session_factory", factory), \
            patch.object(readable, "extract_readable", return_value=(FULL, None, 200, None)):
        await routes._extract_readable_bg(article.id, article.url, None, None, None)

    model.assert_awaited_once()


async def test_retry_button_releases_it(pg, model, spawned):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    with patch.object(readable, "extract_readable_with_title",
                      return_value=readable.ReadableResult(content=FULL)):
        # Unwrapped: the rate limit is not what is tested.
        response = await routes.htmx_extract_readable.__wrapped__(article.id, _request(), user, pg)

    assert "Generating summary" in response.body.decode()
    spawned.assert_called_once()


async def test_no_spinner_until_the_text_is_there(pg, model, spawned):
    user, article = await _setup(pg)
    await _star(pg, user, article)
    polled = await ai_routes.htmx_ai_summary_poll(article.id, user, pg)
    assert "Generating" not in polled.body.decode()
    assert await routes._summary_refresh_oob(
        await routes.get_article(user, article.id, pg), user, pg) == ""

    readable.apply_readable_result(article, FULL, None, 200)
    await pg.flush()
    polled = await ai_routes.htmx_ai_summary_poll(article.id, user, pg)
    assert "Generating" in polled.body.decode()
    assert "Generating" in await routes._summary_refresh_oob(
        await routes.get_article(user, article.id, pg), user, pg)

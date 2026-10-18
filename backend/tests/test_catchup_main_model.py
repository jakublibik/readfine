"""Catch me up runs on the main model only.

The scoring slot holds a deliberately small model (one number per article), so
digests and briefings never touch it, whatever a saved config says.
"""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rate_limit import limiter
from app.routers.web.app import catchup as catchup_router
from app.routers.web.app.catchup import generate_digest


def make_settings(**kwargs):
    defaults = {
        "user_id": 1,
        "timezone": "UTC",
        "ai_quality_provider": "anthropic",
        "ai_quality_model": "claude-sonnet-4-6",
        "ai_fast_provider": "anthropic",
        "ai_fast_model": "claude-haiku-4-5",
        "ai_scoring_enabled_default": False,
        "basic_scoring_enabled": False,
        "ai_preference_text": None,
        # Which slot a digest runs on is what these tests are about, so story folding
        # stays out of the way; it is covered in test_catchup_service.
        "story_dedup": "off",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_execute_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    # The generate endpoint allows one call a minute; without this the second
    # test in the file would get a 429 instead of the behaviour it asserts.
    limiter.reset()
    yield
    limiter.reset()


def _setup(mock_db, settings):
    mock_db.scalar = AsyncMock(return_value=True)  # AI kill-switch on
    mock_db.execute = AsyncMock(return_value=make_execute_result(settings))


ARTICLE = SimpleNamespace(id=1, title="A", feed_title="F")

FORM = {"period": "7days", "filter_status": "all", "article_limit": "10"}
DIGEST = {"period": "7days", "filter_status": "all", "article_limit": 10}


def _patches(client_triple):
    return (
        patch("app.services.catchup_service.validate_scope", new=AsyncMock()),
        patch("app.services.catchup_service.fetch_catchup_articles",
              new=AsyncMock(return_value=[ARTICLE])),
        patch("app.services.catchup_service.apply_catchup_limit",
              return_value=[ARTICLE]),
        patch("app.services.catchup_service.build_articles_meta", return_value=[]),
        patch("app.services.ai_service.get_ai_client",
              new=AsyncMock(return_value=client_triple)),
    )


async def test_generate_without_main_model_errors_and_logs_nothing(mock_db):
    _setup(mock_db, make_settings(ai_quality_provider=None, ai_quality_model=None))
    v, f, a, b, g = _patches((None, None, None))
    with v, f, a, b, g, patch("app.services.ai_service.catch_me_up",
                              new=AsyncMock()) as mock_catchup:
        html = await generate_digest(1, mock_db, **DIGEST)

    assert "No main model configured" in html
    assert "/settings/ai" in html
    mock_catchup.assert_not_called()
    mock_db.add.assert_not_called()  # nothing to log, nothing was spent


async def test_generate_uses_main_model_and_logs_it(mock_db):
    _setup(mock_db, make_settings())
    v, f, a, b, g = _patches((MagicMock(), "anthropic", "claude-sonnet-4-6"))
    with v, f, a, b, g, patch("app.services.ai_service.catch_me_up",
                              new=AsyncMock(return_value=("digest", 100, 50))):
        html = await generate_digest(1, mock_db, **DIGEST)

    assert "digest" in html
    log = mock_db.add.call_args[0][0]
    assert log.model == "claude-sonnet-4-6"
    assert log.model_slot == "quality"


# ── The digest runs as a background job the page polls for ────────────────────

@pytest.fixture
def _jobs():
    catchup_router._digest_jobs.clear()
    yield catchup_router._digest_jobs
    catchup_router._digest_jobs.clear()


def _poll_url(html: str) -> str:
    start = html.index('hx-get="') + len('hx-get="')
    return html[start:html.index('"', start)]


def _wait_for(client, url: str) -> str:
    for _ in range(100):
        text = client.get(url).text
        if "Generating briefing" not in text:
            return text
        time.sleep(0.02)
    raise AssertionError("the digest job never finished")


def _start(client, mock_db, digest):
    _setup(mock_db, make_settings())
    with patch("app.services.catchup_service.validate_scope", new=AsyncMock()), \
            patch.object(catchup_router, "_digest_job", new=digest):
        return client.post("/htmx/catch-me-up/generate", data=FORM)


def test_generate_answers_at_once_and_the_poll_brings_the_digest(client, mock_db, _jobs):
    release = threading.Event()

    async def digest(user_id, **form):
        assert form["period"] == "7days" and form["article_limit"] == 10
        await asyncio.to_thread(release.wait, 5)
        return '<div class="prose">the digest</div>'

    resp = _start(client, mock_db, digest)
    assert resp.status_code == 200
    assert "Generating briefing" in resp.text
    url = _poll_url(resp.text)
    assert "Generating briefing" in client.get(url).text  # still running

    release.set()
    assert "the digest" in _wait_for(client, url)


def test_second_click_while_running_starts_no_second_digest(client, mock_db, _jobs):
    release = threading.Event()
    calls = []

    async def digest(user_id, **form):
        calls.append(user_id)
        await asyncio.to_thread(release.wait, 5)
        return "done"

    first = _start(client, mock_db, digest)
    limiter.reset()  # the per-minute limit would answer the second click on its own
    second = _start(client, mock_db, digest)
    release.set()

    assert _poll_url(second.text) == _poll_url(first.text)
    assert _wait_for(client, _poll_url(first.text)) == "done"
    assert calls == [1]


def test_failed_digest_job_shows_an_error(client, mock_db, _jobs):
    async def digest(user_id, **form):
        raise RuntimeError("boom")

    url = _poll_url(_start(client, mock_db, digest).text)
    assert "Could not generate digest" in _wait_for(client, url)


def test_poll_for_another_users_job_reveals_nothing(client, _jobs):
    _jobs["theirs"] = catchup_router._DigestJob(user_id=99, html="secret digest")
    _jobs["theirs"].finished_at = time.monotonic()

    text = client.get("/htmx/catch-me-up/jobs/theirs").text
    assert "secret digest" not in text
    assert "interrupted" in text


def test_poll_for_unknown_job_says_it_was_interrupted(client, _jobs):
    assert "Generate it again" in client.get("/htmx/catch-me-up/jobs/gone").text


def test_finished_digests_are_forgotten_after_a_while(_jobs):
    old = catchup_router._DigestJob(user_id=1, html="old")
    old.finished_at = time.monotonic() - catchup_router._DIGEST_KEEP_SECONDS - 1
    running = catchup_router._DigestJob(user_id=1)
    _jobs.update(old=old, running=running)

    catchup_router._forget_old_digests()
    assert list(_jobs) == ["running"]

"""Catch me up runs on the main model only.

The scoring slot holds a deliberately small model (one number per article), so
digests and briefings never touch it, whatever a saved config says.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.rate_limit import limiter


def make_settings(**kwargs):
    defaults = {
        "user_id": 1,
        "timezone": "UTC",
        "ai_quality_provider": "anthropic",
        "ai_quality_model": "claude-sonnet-4-6",
        "ai_fast_provider": "anthropic",
        "ai_fast_model": "claude-haiku-4-5",
        "ai_scoring_enabled_default": False,
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


def test_generate_without_main_model_errors_and_logs_nothing(client, mock_db):
    _setup(mock_db, make_settings(ai_quality_provider=None, ai_quality_model=None))
    v, f, a, b, g = _patches((None, None, None))
    with v, f, a, b, g, patch("app.services.ai_service.catch_me_up",
                              new=AsyncMock()) as mock_catchup:
        resp = client.post("/htmx/catch-me-up/generate", data=FORM)

    assert resp.status_code == 200
    assert "No main model configured" in resp.text
    assert "/settings/ai" in resp.text
    mock_catchup.assert_not_called()
    mock_db.add.assert_not_called()  # nothing to log, nothing was spent


def test_generate_uses_main_model_and_logs_it(client, mock_db):
    _setup(mock_db, make_settings())
    v, f, a, b, g = _patches((MagicMock(), "anthropic", "claude-sonnet-4-6"))
    with v, f, a, b, g, patch("app.services.ai_service.catch_me_up",
                              new=AsyncMock(return_value=("digest", 100, 50))):
        resp = client.post("/htmx/catch-me-up/generate", data=FORM)

    assert resp.status_code == 200
    assert "digest" in resp.text
    log = mock_db.add.call_args[0][0]
    assert log.model == "claude-sonnet-4-6"
    assert log.model_slot == "quality"

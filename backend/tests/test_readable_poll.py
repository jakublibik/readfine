"""Readable-poll endpoint: while an extraction runs it must swap only the small
progress strip (swapping the whole article every 2s made the page jump), and when
it finishes it must hand back the full content plus whatever the AI pipeline
produced meanwhile."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    from app.rate_limit import limiter
    limiter.reset()
    yield


def make_article(**kwargs):
    defaults = {
        "id": 10,
        "title": "Test Article",
        "url": "https://example.com/a",
        "content": "<p>feed body</p>",
        "readable_content": None,
        "readable_status": "pending",
        "readable_error": None,
        "readable_active": True,
        "estimated_read_min": 3,
        "published_at": None,
        "labels": [],
        "is_starred": True,
        "is_archived": False,
        "ai_summary": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _no_ai():
    return AsyncMock(return_value=SimpleNamespace(
        ai_on=False, quality=False, chat=False, catchup=False
    ))


def _with_ai():
    return AsyncMock(return_value=SimpleNamespace(
        ai_on=True, quality=True, chat=False, catchup=False
    ))


class TestReadablePoll:
    def test_running_extraction_returns_only_the_progress_strip(self, client, mock_db):
        with patch("app.routers.web.app.articles.get_article",
                   new=AsyncMock(return_value=make_article())):
            resp = client.get("/htmx/articles/10/readable-poll")
        assert resp.status_code == 200
        assert 'id="readable-poll-10"' in resp.text
        assert "Extracting full content" in resp.text
        # The article body must stay untouched — no content block, no retarget.
        assert 'id="article-content-10"' not in resp.text
        assert "feed body" not in resp.text
        assert "HX-Retarget" not in resp.headers

    def test_finished_extraction_returns_content_and_retargets(self, client, mock_db):
        article = make_article(
            readable_active=False, readable_status="success",
            readable_content="<p>readable body</p>",
        )
        with (
            patch("app.routers.web.app.articles.get_article", new=AsyncMock(return_value=article)),
            patch("app.routers.web.app.articles._ai_availability", new=_no_ai()),
        ):
            resp = client.get("/htmx/articles/10/readable-poll")
        assert resp.status_code == 200
        assert resp.headers["HX-Retarget"] == "#article-content-10"
        assert resp.headers["HX-Reswap"] == "outerHTML"
        assert 'id="article-content-10"' in resp.text
        assert "readable body" in resp.text
        assert 'id="readable-poll-10"' not in resp.text  # polling stops

    def test_summary_produced_during_extraction_is_swapped_in(self, client, mock_db):
        """The pipeline runs when extraction finishes, so a summary can appear after
        the article was rendered — it must not wait for a reopen."""
        article = make_article(
            readable_active=False, readable_status="success",
            readable_content="<p>readable body</p>", ai_summary="The summary.",
        )
        with (
            patch("app.routers.web.app.articles.get_article", new=AsyncMock(return_value=article)),
            patch("app.routers.web.app.articles._ai_availability", new=_with_ai()),
        ):
            resp = client.get("/htmx/articles/10/readable-poll")
        assert resp.status_code == 200
        assert 'id="ai-summary-10"' in resp.text
        assert 'hx-swap-oob="true"' in resp.text
        assert "The summary." in resp.text

    def test_pending_summary_job_swaps_in_the_spinner(self, client, mock_db):
        article = make_article(readable_active=False, readable_status="success",
                               readable_content="<p>readable body</p>")
        mock_db.scalar = AsyncMock(side_effect=[None, 42])  # settings, pending job id
        with (
            patch("app.routers.web.app.articles.get_article", new=AsyncMock(return_value=article)),
            patch("app.routers.web.app.articles._ai_availability", new=_with_ai()),
        ):
            resp = client.get("/htmx/articles/10/readable-poll")
        assert resp.status_code == 200
        assert "Generating summary" in resp.text
        assert "/htmx/articles/10/ai-summary/poll" in resp.text

    def test_no_summary_and_no_job_leaves_the_block_alone(self, client, mock_db):
        article = make_article(readable_active=False, readable_status="success",
                               readable_content="<p>readable body</p>")
        mock_db.scalar = AsyncMock(side_effect=[None, None])  # settings, no job
        with (
            patch("app.routers.web.app.articles.get_article", new=AsyncMock(return_value=article)),
            patch("app.routers.web.app.articles._ai_availability", new=_with_ai()),
        ):
            resp = client.get("/htmx/articles/10/readable-poll")
        assert resp.status_code == 200
        assert 'id="ai-summary-10"' not in resp.text

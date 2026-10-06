"""How a failed fetch is reported to someone subscribing (app.fetcher.errors).

Subscribe used to let httpx's exceptions out, so the web form matched words in its
own messages to decide what to offer, and the API answered a 404 at the feed's
address with a 500.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.fetcher.errors import NotAFeed, describe_fetch_error
from app.services.feed import FeedFetchError, FeedSubscriptionError
from app.utils.url_validator import ResponseTooLarge
from tests.test_feed_url_credentials import _subscribe_db


def _status_error(status, headers=None):
    request = httpx.Request("GET", "https://example.com/feed")
    response = httpx.Response(status, headers=headers or {}, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


class TestDescribeFetchError:
    @pytest.mark.parametrize("status,phrase", [
        (401, "Authentication required"),
        (403, "Access denied"),
        (404, "Not found"),
        (503, "temporarily down"),
        (418, "HTTP 418"),
    ])
    def test_http_statuses(self, status, phrase):
        problem = describe_fetch_error(_status_error(status))
        assert problem.kind == "http"
        assert problem.status == status
        assert phrase in problem.message

    def test_rate_limit_says_when_to_come_back(self):
        problem = describe_fetch_error(_status_error(429, {"retry-after": "30"}))
        assert "Try again in" in problem.message
        assert "sec" in problem.message

    def test_timeout_is_not_called_a_dropped_connection(self):
        problem = describe_fetch_error(httpx.ReadTimeout("slow"))
        assert problem.kind == "timeout"

    def test_dropped_connection(self):
        problem = describe_fetch_error(httpx.RemoteProtocolError("Server disconnected"))
        assert problem.kind == "transport"
        assert "closed the connection" in problem.message

    def test_too_large(self):
        assert describe_fetch_error(ResponseTooLarge("too big")).kind == "too_large"

    def test_not_a_feed(self):
        problem = describe_fetch_error(NotAFeed("The server returned a web page, not a feed"))
        assert problem.kind == "not_feed"
        assert problem.message == "The server returned a web page, not a feed"

    def test_a_refused_address_keeps_its_sentence(self):
        problem = describe_fetch_error(ValueError("Redirect blocked: private address"))
        assert problem.kind == "invalid"
        assert problem.message == "Redirect blocked: private address"

    def test_our_own_bug_is_not_a_fetch_failure(self):
        assert describe_fetch_error(RuntimeError("broken query")) is None


class TestWrongKindOfPage:
    """Whether to look for a linked feed or offer a scrape setup."""

    @pytest.mark.parametrize("exc,expected", [
        (NotAFeed("web page"), True),
        (_status_error(404), True),
        (_status_error(403), True),
        (_status_error(401), False),
        (_status_error(429), False),
        (_status_error(502), False),
        (httpx.ReadTimeout("slow"), False),
        (ValueError("blocked"), False),
    ])
    def test_offer(self, exc, expected):
        assert describe_fetch_error(exc).wrong_kind_of_page is expected


class TestSubscribeRaisesFeedFetchError:
    async def _subscribe(self, fetch_error):
        from app.services.feed import subscribe
        from tests.conftest import make_mock_user

        with (
            patch("app.services.feed.async_validate_feed_url", new=AsyncMock()),
            patch("app.services.feed.fetch_and_parse_url",
                  new=AsyncMock(side_effect=fetch_error)),
        ):
            await subscribe(
                user=make_mock_user(role="admin"), url="https://example.com/feed",
                folder_id=None, custom_title=None, fetch_auth_user=None,
                fetch_auth_pass=None, db=_subscribe_db(), trigger_initial_fetch=False,
            )

    async def test_http_error_becomes_a_subscription_error(self):
        # A FeedSubscriptionError is a ValueError, which the API answers with 400.
        with pytest.raises(FeedFetchError) as info:
            await self._subscribe(_status_error(404))
        assert isinstance(info.value, FeedSubscriptionError)
        assert isinstance(info.value, ValueError)
        assert info.value.problem.status == 404
        assert "Not found (404)" in str(info.value)

    async def test_timeout_becomes_a_subscription_error(self):
        with pytest.raises(FeedFetchError) as info:
            await self._subscribe(httpx.ConnectTimeout("slow"))
        assert info.value.problem.kind == "timeout"

    async def test_our_own_bug_is_not_dressed_up(self):
        with pytest.raises(RuntimeError):
            await self._subscribe(RuntimeError("broken query"))

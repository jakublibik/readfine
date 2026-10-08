"""Tests for custom HTML error pages (404, 500) and API JSON fallback."""
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def error_client(mock_db):
    from app.main import app
    from app.database import get_db

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


class TestErrorPages404:
    def test_unknown_web_route_returns_html_404(self, error_client):
        r = error_client.get("/this-does-not-exist")
        assert r.status_code == 404
        assert "text/html" in r.headers.get("content-type", "")
        assert "404" in r.text

    def test_unknown_web_route_has_back_link(self, error_client):
        r = error_client.get("/this-does-not-exist")
        assert "/app" in r.text

    def test_unknown_api_route_returns_json_404(self, error_client):
        r = error_client.get("/api/v1/nonexistent-endpoint")
        assert r.status_code == 404
        assert "application/json" in r.headers.get("content-type", "")

    def test_unknown_api_route_does_not_return_html(self, error_client):
        r = error_client.get("/api/v1/nonexistent-endpoint")
        assert "<html" not in r.text


class TestErrorPages500:
    # The @app.exception_handler(Exception) is registered in ExceptionMiddleware,
    # but Starlette's BaseHTTPMiddleware bypasses it for dependency-level exceptions.
    # We test the handler function directly to verify its contract.

    def test_500_handler_returns_html_response(self):
        import asyncio
        from unittest.mock import MagicMock
        from starlette.datastructures import State
        from app.main import app

        handler = app.exception_handlers.get(Exception)
        assert handler is not None, "Exception handler must be registered"

        request = MagicMock()
        request.url.path = "/app"  # web path → HTML branch
        request.state = State()
        request.state.csp_nonce = "test-nonce"

        response = asyncio.run(
            handler(request, RuntimeError("boom"))
        )
        assert response.status_code == 500

    def test_500_handler_does_not_leak_exception_detail(self):
        import asyncio
        from unittest.mock import MagicMock
        from starlette.datastructures import State
        from app.main import app

        handler = app.exception_handlers.get(Exception)
        request = MagicMock()
        request.url.path = "/app"  # web path → HTML branch
        request.state = State()
        request.state.csp_nonce = "test-nonce"

        response = asyncio.run(
            handler(request, RuntimeError("secret internal detail"))
        )
        body = b"".join(response.body_iterator if hasattr(response, "body_iterator") else [response.body])
        assert b"secret internal detail" not in body
        assert b"500" in body

    def test_500_handler_returns_json_for_api(self):
        import asyncio
        from unittest.mock import MagicMock
        from app.main import app

        handler = app.exception_handlers.get(Exception)
        request = MagicMock()
        request.url.path = "/api/v1/feeds"

        response = asyncio.run(handler(request, RuntimeError("secret internal detail")))
        assert response.status_code == 500
        assert response.media_type == "application/json"
        assert b"secret internal detail" not in response.body
        assert b"detail" in response.body


class TestRateLimitErrorShape:
    def test_429_handler_returns_json_for_api(self):
        import asyncio
        from unittest.mock import MagicMock
        from slowapi.errors import RateLimitExceeded
        from app.main import app

        handler = app.exception_handlers.get(RateLimitExceeded)
        assert handler is not None, "RateLimitExceeded handler must be registered"

        request = MagicMock()
        request.url.path = "/api/v1/tokens"

        # The handler ignores the exception object; only the request path matters.
        response = asyncio.run(handler(request, MagicMock(spec=RateLimitExceeded)))
        assert response.status_code == 429
        assert response.media_type == "application/json"
        assert b"detail" in response.body

    def test_429_handler_returns_html_for_web(self):
        import asyncio
        from unittest.mock import MagicMock
        from slowapi.errors import RateLimitExceeded
        from starlette.datastructures import State
        from app.main import app

        handler = app.exception_handlers.get(RateLimitExceeded)
        request = MagicMock()
        request.url.path = "/login"
        request.state = State()
        request.state.csp_nonce = "test-nonce"

        response = asyncio.run(handler(request, MagicMock(spec=RateLimitExceeded)))
        assert response.status_code == 429
        assert response.media_type != "application/json"


class TestErrorPages401Regression:
    def test_unauthenticated_web_route_redirects_to_login(self, error_client):
        r = error_client.get("/app", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")


class TestClientErrorSafetyNets:
    """Validation and unique-index errors that slip past a route are the user's input,
    so they answer 400/409 with a message, not the 500 page."""

    def _request(self, path, htmx=False):
        from unittest.mock import MagicMock
        from starlette.datastructures import State
        request = MagicMock()
        request.url.path = path
        request.method = "POST"
        request.headers = {"HX-Request": "true"} if htmx else {}
        request.state = State()
        request.state.csp_nonce = "test-nonce"
        return request

    def _validation_error(self):
        from pydantic import BaseModel, Field, ValidationError
        class Form(BaseModel):
            name: str = Field(max_length=3)
        try:
            Form(name="too long")
        except ValidationError as exc:
            return exc

    def _integrity_error(self, code):
        from types import SimpleNamespace
        from sqlalchemy.exc import IntegrityError
        return IntegrityError("INSERT ...", {}, SimpleNamespace(sqlstate=code))

    def _handler(self, exc_class):
        from app.main import app
        return app.exception_handlers[exc_class]

    def test_validation_error_is_400_json_for_api(self):
        import asyncio, json
        from pydantic import ValidationError
        response = asyncio.run(self._handler(ValidationError)(
            self._request("/api/v1/labels"), self._validation_error()))
        assert response.status_code == 400
        assert "name" in json.loads(response.body)["detail"]

    def test_validation_error_is_toast_for_htmx(self):
        import asyncio, json
        from pydantic import ValidationError
        response = asyncio.run(self._handler(ValidationError)(
            self._request("/app/labels", htmx=True), self._validation_error()))
        assert response.status_code == 400
        toast = json.loads(response.headers["HX-Trigger"])["showToast"]
        assert toast["type"] == "error"
        assert "name" in toast["msg"]

    def test_validation_error_is_page_for_plain_web(self):
        import asyncio
        from pydantic import ValidationError
        response = asyncio.run(self._handler(ValidationError)(
            self._request("/settings/labels"), self._validation_error()))
        assert response.status_code == 400
        assert b"Invalid name" in response.body

    def test_unique_violation_is_409(self):
        import asyncio, json
        from sqlalchemy.exc import IntegrityError
        response = asyncio.run(self._handler(IntegrityError)(
            self._request("/api/v1/folders"), self._integrity_error("23505")))
        assert response.status_code == 409
        assert "already exists" in json.loads(response.body)["detail"]

    def test_other_integrity_error_stays_500(self):
        import asyncio
        from sqlalchemy.exc import IntegrityError
        response = asyncio.run(self._handler(IntegrityError)(
            self._request("/api/v1/folders"), self._integrity_error("23503")))
        assert response.status_code == 500
        assert b"INSERT" not in response.body

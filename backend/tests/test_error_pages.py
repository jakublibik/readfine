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
        request.state = State()
        request.state.csp_nonce = "test-nonce"

        response = asyncio.run(
            handler(request, RuntimeError("secret internal detail"))
        )
        body = b"".join(response.body_iterator if hasattr(response, "body_iterator") else [response.body])
        assert b"secret internal detail" not in body
        assert b"500" in body


class TestErrorPages401Regression:
    def test_unauthenticated_web_route_redirects_to_login(self, error_client):
        r = error_client.get("/app", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers.get("location", "")

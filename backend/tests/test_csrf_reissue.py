"""A csrftoken cookie signed with another secret is replaced, not kept forever."""
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from starlette_csrf import CSRFMiddleware

from app.auth.csrf import ReissuingCSRFMiddleware

SECRET = "this-instance-secret"


def _client() -> TestClient:
    async def page(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", page, methods=["GET", "POST"])])
    app.add_middleware(ReissuingCSRFMiddleware, secret=SECRET, sensitive_cookies={"session"})
    return TestClient(app)


def _foreign_token() -> str:
    """A well-formed token from another Readfine (different SECRET_KEY)."""
    return CSRFMiddleware(app=None, secret="another-instance-secret")._generate_csrf_token()


def _set_cookies(client: TestClient, csrftoken: str) -> None:
    client.cookies.set("session", "x")
    client.cookies.set("csrftoken", csrftoken)


def test_foreign_cookie_is_replaced_on_get_and_next_post_passes():
    client = _client()
    _set_cookies(client, _foreign_token())

    r = client.get("/")
    assert r.status_code == 200
    fresh = r.cookies.get("csrftoken")
    assert fresh

    _set_cookies(client, fresh)
    assert client.post("/", headers={"x-csrftoken": fresh}).status_code == 200


def test_post_with_foreign_cookie_is_still_rejected_but_gets_a_fresh_one():
    client = _client()
    foreign = _foreign_token()
    _set_cookies(client, foreign)

    r = client.post("/", headers={"x-csrftoken": foreign})
    assert r.status_code == 403
    fresh = r.cookies.get("csrftoken")
    assert fresh and fresh != foreign

    _set_cookies(client, fresh)
    assert client.post("/", headers={"x-csrftoken": fresh}).status_code == 200


def test_garbage_cookie_is_replaced():
    client = _client()
    _set_cookies(client, "not-a-token")
    assert client.get("/").cookies.get("csrftoken")


def test_valid_cookie_is_left_alone():
    client = _client()
    token = client.get("/").cookies.get("csrftoken")
    _set_cookies(client, token)

    r = client.post("/", headers={"x-csrftoken": token})
    assert r.status_code == 200
    assert "set-cookie" not in r.headers


def test_post_without_header_is_rejected():
    client = _client()
    token = client.get("/").cookies.get("csrftoken")
    _set_cookies(client, token)
    assert client.post("/").status_code == 403

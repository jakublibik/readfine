"""Security-header middleware: authenticated HTML must be uncacheable (CWE-525),
static assets must stay cacheable."""


def test_html_response_is_no_store(unauth_client):
    """HTML pages carry Cache-Control: no-store + Vary: Cookie so a shared browser
    can't show a previous user's rendered page (bfcache / back-forward)."""
    resp = unauth_client.get("/login")
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") == "no-store"
    assert resp.headers.get("vary") == "Cookie"


def test_static_asset_stays_cacheable(unauth_client):
    """Static assets are not text/html and must not get no-store."""
    resp = unauth_client.get("/static/js/ai-settings.js")
    assert resp.status_code == 200
    assert not resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") != "no-store"


def test_json_response_not_no_store(unauth_client):
    """Non-HTML (JSON) responses are untouched by the HTML no-store rule."""
    resp = unauth_client.get("/healthz")
    assert not resp.headers["content-type"].startswith("text/html")
    assert resp.headers.get("cache-control") != "no-store"

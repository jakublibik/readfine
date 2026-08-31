"""PWA plumbing: the manifest must stay reachable, public and honest about itself.

Not a route test for its own sake — a manifest that 404s or arrives with the wrong
content type fails silently. The browser simply never offers to install the app, and
nothing in the UI says why.
"""
import json


def test_manifest_is_served_unauthenticated(unauth_client):
    """Fetched outside normal navigation, so it must not sit behind the session."""
    resp = unauth_client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")


def test_manifest_start_url_is_inside_scope(unauth_client):
    """start_url outside scope makes the app uninstallable, with no visible error."""
    m = json.loads(unauth_client.get("/manifest.webmanifest").content)
    assert m["start_url"].startswith(m["scope"])
    assert m["display"] == "standalone"


def test_manifest_icons_exist(unauth_client):
    """Every icon must resolve; a 404 here silently drops installability."""
    m = json.loads(unauth_client.get("/manifest.webmanifest").content)
    purposes = {icon["purpose"] for icon in m["icons"]}
    assert {"any", "maskable"} <= purposes
    for icon in m["icons"]:
        assert unauth_client.get(icon["src"]).status_code == 200, icon["src"]

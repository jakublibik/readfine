"""CSP without 'unsafe-eval', and templates that do not depend on htmx eval.

htmx runs with allowEval off, so an hx-on attribute, an hx-vals="js:..." value or an
hx-trigger event filter would not raise anything: it would just quietly do nothing.
The template scan is there to catch that before a browser does.
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


@pytest.fixture
def client(mock_db):
    from app.main import app
    from app.database import get_db

    async def override_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


def test_csp_has_no_unsafe_eval(client):
    r = client.get("/login")
    csp = r.headers["Content-Security-Policy"]
    script_src = next(p for p in csp.split(";") if p.strip().startswith("script-src"))
    assert "'unsafe-eval'" not in script_src
    assert "'unsafe-inline'" not in script_src
    assert "'nonce-" in script_src


def test_htmx_eval_off_and_no_nonce_for_swapped_scripts(client):
    body = client.get("/login").text
    assert "htmx.config.allowEval = false" in body
    assert "inlineScriptNonce" not in body


_EVAL_PATTERNS = [
    (re.compile(r"\bhx-on[:-]"), "hx-on"),
    (re.compile(r"""hx-(vals|vars|headers)\s*=\s*["']\s*(js|javascript):"""), "js: value"),
    (re.compile(r"""hx-trigger\s*=\s*["'][^"']*\["""), "hx-trigger filter"),
]


def test_templates_do_not_use_htmx_eval():
    found = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        # Jinja comments may mention the attributes by name. Their newlines stay, so
        # the reported line numbers are right.
        text = re.sub(r"\{#.*?#\}", lambda m: "\n" * m.group().count("\n"), text, flags=re.S)
        for pattern, what in _EVAL_PATTERNS:
            for m in pattern.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                found.append(f"{path.relative_to(TEMPLATES)}:{line} {what}")
    assert not found, "htmx runs with allowEval off; move these to JS:\n" + "\n".join(found)

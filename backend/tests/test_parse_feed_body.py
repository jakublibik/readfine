"""parse_feed_body must treat a downloaded body as content, never as an address (H2-01).

feedparser.parse downloads a str that looks like a URL and opens one that looks
like a path, so a feed host answering with an internal address as its whole body
would make us fetch it, outside every SSRF check.
"""
import http.server
import re
import threading
from pathlib import Path

import pytest

from app.utils.parsing import parse_feed_body

_RSS = (
    '<?xml version="1.0"?><rss version="2.0"><channel><title>INTERNAL</title>'
    "<item><title>secret item</title><link>http://internal/1</link></item>"
    "</channel></rss>"
)


@pytest.fixture
def internal_server():
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/rss+xml")
            self.end_headers()
            self.wfile.write(_RSS.encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", hits
    finally:
        server.shutdown()


@pytest.mark.parametrize("prefix", ["", "  ", "\n"])
def test_url_body_is_not_fetched(internal_server, prefix):
    base, hits = internal_server
    parsed = parse_feed_body(f"{prefix}{base}/admin")
    assert hits == []
    assert not parsed.entries
    assert parsed.feed.get("title") is None


def test_path_body_is_not_opened(tmp_path: Path):
    local = tmp_path / "local.xml"
    local.write_text(_RSS)
    for body in (str(local), local.as_uri()):
        parsed = parse_feed_body(body)
        assert not parsed.entries, body


def test_real_feed_still_parses():
    parsed = parse_feed_body(_RSS)
    assert parsed.feed.title == "INTERNAL"
    assert [e.title for e in parsed.entries] == ["secret item"]


_TITLE = "Příliš žluťoučký kůň"


def _cp1250_feed(declared: bool) -> bytes:
    decl = ' encoding="windows-1250"' if declared else ""
    return (
        f'<?xml version="1.0"{decl}?><rss version="2.0"><channel><title>{_TITLE}</title>'
        "<item><title>x</title></item></channel></rss>"
    ).encode("windows-1250")


@pytest.mark.parametrize(
    ("declared", "content_type"),
    [
        # The H2-05 case: the charset is only in the XML declaration.
        (True, "application/rss+xml"),
        (True, "text/xml"),
        (True, None),
        # Only in the header, which a decoded str used to cover and bytes alone do not.
        (False, "text/xml; charset=windows-1250"),
        (True, "application/xml; charset=windows-1250"),
    ],
)
def test_non_utf8_feed_decodes(declared, content_type):
    parsed = parse_feed_body(_cp1250_feed(declared), content_type)
    assert parsed.feed.title == _TITLE


def test_str_body_parses_as_utf8():
    assert parse_feed_body(_RSS.replace("INTERNAL", _TITLE)).feed.title == _TITLE


def test_app_never_hands_feedparser_a_body_directly():
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders = [
        f"{path.relative_to(app_dir)}:{n}"
        for path in app_dir.rglob("*.py")
        if path.name != "parsing.py"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"feedparser\.parse\b", line)
    ]
    assert offenders == [], "use app.utils.parsing.parse_feed_body instead"

"""The share menu's "Original article" option copies the wrong link if the markup and
the JS selector drift apart.

`app.js` reads the source URL off the article element it finds in the detail pane. That
worked until a wrapper div carrying `data-article-id` was added above the `<article>`:
a bare `[data-article-id]` query then matched the wrapper, found no `data-url`, and fell
back to `window.location.href`, so users copied a Readfine URL instead of the article's.
The fix pins the selector to `article[data-article-id]`; these tests pin both ends of
that contract so the next wrapper doesn't silently break it again."""
import re
from pathlib import Path
from types import SimpleNamespace

from bs4 import BeautifulSoup

from app.templating import templates

APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "app.js"

ARTICLE_URL = "https://example.com/original-article"
ARTICLE_TITLE = "Original title"


def _render_detail() -> str:
    article = SimpleNamespace(
        id=7,
        title=ARTICLE_TITLE,
        url=ARTICLE_URL,
        feed_title="Example feed",
        author=None,
        labels=[],
        content=None,
        readable_status="success",
        readable_content="<p>Body</p>",
        readable_error=None,
        readable_active=False,
        estimated_read_min=3,
        is_read=False,
        is_starred=False,
        is_archived=False,
        share_token=None,
        ai_summary=None,
        ai_context=None,
    )
    return templates.env.get_template("app/partials/article_detail.html").render(
        article=article,
        request=SimpleNamespace(base_url="https://readfine.test/"),
        ai_available=False,
        chat_available=False,
    )


def test_article_element_carries_the_source_url():
    # This is the element the share handler reads; without data-url it falls back to
    # the current page and the user copies a Readfine link.
    soup = BeautifulSoup(_render_detail(), "lxml")
    el = soup.select_one("article[data-article-id]")
    assert el is not None
    assert el["data-url"] == ARTICLE_URL
    assert el["data-title"] == ARTICLE_TITLE


def test_wrapper_above_the_article_does_not_answer_for_it():
    # A wrapper with data-article-id is fine (dwell tracking uses it) as long as it
    # isn't mistaken for the article itself, so assert it carries no url/title.
    soup = BeautifulSoup(_render_detail(), "lxml")
    for el in soup.select("[data-article-id]"):
        if el.name == "article":
            continue
        assert not el.has_attr("data-url")
        assert not el.has_attr("data-title")


def test_share_handlers_query_the_article_tag():
    source = APP_JS.read_text(encoding="utf-8")
    assert "#article-detail article[data-article-id]" in source
    assert "#inline-article-detail-content article[data-article-id]" in source


def test_bare_detail_selectors_are_only_used_for_the_id():
    # A bare '[data-article-id]' inside the detail containers matches the wrapper
    # first, in document order. That is fine for reading the id (the wrapper carries
    # it) and wrong for anything else, which is exactly how the bug got in.
    source = APP_JS.read_text(encoding="utf-8")
    pattern = re.compile(
        r"#(?:article-detail|inline-article-detail-content)\s+\[data-article-id\]"
    )
    for match in pattern.finditer(source):
        following = source[match.end():match.end() + 400]
        for attr in ("dataset.url", "dataset.title", "dataset.isRead"):
            assert attr not in following, (
                f"{match.group(0)} may match the wrapper; it must not be used to read {attr}"
            )

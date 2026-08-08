"""Tests for web scraping feed type: extract_article_links, generate_selector_prompt,
fetch_scrape_feed (error handling), and subscribe_scrape service."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.fetcher.scrape import (
    _excerpt_to_content_html,
    _extract_excerpt,
    _extract_published_at,
    _metadata_context,
    _published_at_from_url,
    extract_article_links,
    fetch_scrape_feed,
)
from app.fetcher.failure import BLOCK_BACKOFF_BASE
from app.models.fetch_log import FetchLog
from app.utils.scrape_ai import generate_selector_prompt
from app.utils.url_validator import PageResponse


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_scrape_feed(**kwargs) -> SimpleNamespace:
    defaults = {
        "id": 1,
        "feed_url": "https://example.com/news",
        "feed_type": "scrape",
        "is_private": False,
        "fetch_auth_user": None,
        "fetch_auth_pass_encrypted": None,
        "fetch_error_count": 0,
        "block_count": 0,
        "status": "active",
        "last_fetched_at": None,
        "last_fetch_duration_ms": None,
        "last_error": None,
        "retry_after_until": None,
        "type_config": {"article_links_selector": "article.item h2 a"},
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _make_session() -> AsyncMock:
    session = AsyncMock()
    session.add = MagicMock()
    result = MagicMock()
    result.scalars.return_value = MagicMock(return_value=[])
    session.execute = AsyncMock(return_value=result)
    return session


def _page(text: str, permanent_url: str | None = None) -> PageResponse:
    """A fetched page, optionally one that permanently redirected."""
    return PageResponse(text, permanent_url, permanent_url or "https://example.com/feed")


_HTML_WITH_ARTICLES = """
<html><body>
  <nav><a href="/about">About</a></nav>
  <main>
    <article class="item">
      <h2><a href="/news/1">Article One</a></h2>
      <time datetime="2024-03-15T10:00:00Z">March 15</time>
      <p>First article summary text here.</p>
    </article>
    <article class="item">
      <h2><a href="/news/2">Article Two</a></h2>
      <time datetime="2024-03-14T08:30:00+02:00">March 14</time>
      <p>Second article summary text here.</p>
    </article>
    <article class="item"><h2><a href="/news/3">Article Three</a></h2><p>Summary</p></article>
  </main>
  <footer><a href="/privacy">Privacy</a></footer>
</body></html>
"""


# ── _extract_published_at ─────────────────────────────────────────────────────

class TestExtractPublishedAt:
    def _elem(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml").find("article")

    def test_returns_datetime_from_time_tag(self):
        elem = self._elem('<article><time datetime="2024-03-15T10:00:00Z">text</time></article>')
        result = _extract_published_at(elem)
        assert result == datetime(2024, 3, 15, 10, 0, 0, tzinfo=timezone.utc)

    def test_offset_aware_datetime_preserved(self):
        elem = self._elem('<article><time datetime="2024-03-14T08:30:00+02:00">text</time></article>')
        result = _extract_published_at(elem)
        assert result is not None
        assert result.utcoffset() is not None

    def test_no_time_tag_returns_none(self):
        elem = self._elem('<article><p>No time here</p></article>')
        assert _extract_published_at(elem) is None

    def test_time_without_datetime_attr_returns_none(self):
        elem = self._elem('<article><time>March 15</time></article>')
        assert _extract_published_at(elem) is None

    def test_invalid_datetime_returns_none(self):
        elem = self._elem('<article><time datetime="not-a-date">text</time></article>')
        assert _extract_published_at(elem) is None

    def test_naive_datetime_returns_none(self):
        elem = self._elem('<article><time datetime="2024-03-15T10:00:00">text</time></article>')
        assert _extract_published_at(elem) is None


# ── _published_at_from_url ────────────────────────────────────────────────────

class TestPublishedAtFromUrl:
    def test_irozhlas_pattern(self):
        url = "https://www.irozhlas.cz/zpravy-svet/nato_2605091200_ula"
        result = _published_at_from_url(url)
        assert result == datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)

    def test_older_date(self):
        url = "https://www.irozhlas.cz/zpravy-svet/tema_2604301644_jos"
        result = _published_at_from_url(url)
        assert result == datetime(2026, 4, 30, 16, 44, tzinfo=timezone.utc)

    def test_no_pattern_returns_none(self):
        assert _published_at_from_url("https://www.irozhlas.cz/zpravy-svet") is None

    def test_wrong_length_returns_none(self):
        assert _published_at_from_url("https://example.com/article_260509_slug") is None


# ── _extract_excerpt ──────────────────────────────────────────────────────────

class TestExtractExcerpt:
    def _elem(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml").find("article")

    def test_returns_first_p_text(self):
        elem = self._elem('<article><h2>Title</h2><p>This is a longer excerpt text here.</p></article>')
        result = _extract_excerpt(elem, "Title")
        assert result == "This is a longer excerpt text here."

    def test_skips_short_p(self):
        elem = self._elem('<article><p>Short.</p><p>This is a longer excerpt text here.</p></article>')
        result = _extract_excerpt(elem, "")
        assert result == "This is a longer excerpt text here."

    def test_skips_title_duplicate(self):
        title = "Exact Title Text Here Long Enough To Pass"
        elem = self._elem(f'<article><p>{title}</p><p>This is the real article excerpt text.</p></article>')
        result = _extract_excerpt(elem, title)
        assert result == "This is the real article excerpt text."

    def test_skips_pipe_metadata(self):
        elem = self._elem('<article><p>Location|12:00|Author|Section</p><p>This is the real article excerpt text.</p></article>')
        result = _extract_excerpt(elem, "")
        assert result == "This is the real article excerpt text."

    def test_no_p_returns_none(self):
        elem = self._elem('<article><h2>Title</h2><span>no p tag</span></article>')
        assert _extract_excerpt(elem, "Title") is None

    def test_all_p_too_short_returns_none(self):
        elem = self._elem('<article><p>Hi.</p><p>Bye.</p></article>')
        assert _extract_excerpt(elem, "") is None

    def test_truncates_at_500(self):
        long_text = "word " * 200
        elem = self._elem(f'<article><p>{long_text}</p></article>')
        result = _extract_excerpt(elem, "")
        assert result is not None
        assert len(result) <= 500


# ── _excerpt_to_content_html (stored XSS guard) ────────────────────────────────

class TestExcerptToContentHtml:
    def test_none_excerpt_returns_none(self):
        assert _excerpt_to_content_html(None) is None

    def test_empty_excerpt_returns_none(self):
        assert _excerpt_to_content_html("") is None

    def test_plain_text_wrapped_in_paragraph(self):
        assert _excerpt_to_content_html("Hello world") == "<p>Hello world</p>"

    def test_script_tag_is_escaped(self):
        # A scraped page can yield decoded markup as excerpt text — it must be
        # escaped, never stored as live HTML (rendered `| safe` at read time).
        result = _excerpt_to_content_html('<script>alert(1)</script>')
        assert "<script>" not in result
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in result

    def test_angle_brackets_and_quotes_escaped(self):
        result = _excerpt_to_content_html('<img src=x onerror="alert(1)">')
        assert "<img" not in result
        assert "&lt;img" in result


# ── _metadata_context ─────────────────────────────────────────────────────────

class TestMetadataContext:
    def _soup(self, html):
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "lxml")

    def test_non_a_elem_returns_itself(self):
        soup = self._soup('<article><h2><a href="/x">T</a></h2></article>')
        article = soup.find("article")
        assert _metadata_context(article) is article

    def test_a_elem_returns_article_parent(self):
        soup = self._soup('<article><h2><a href="/x">Title</a></h2></article>')
        a = soup.find("a")
        ctx = _metadata_context(a)
        assert ctx.name == "article"

    def test_a_elem_returns_li_parent(self):
        soup = self._soup('<ul><li><a href="/x">Title</a></li></ul>')
        a = soup.find("a")
        ctx = _metadata_context(a)
        assert ctx.name == "li"

    def test_a_elem_returns_div_parent(self):
        soup = self._soup('<div class="card"><a href="/x">Title</a></div>')
        a = soup.find("a")
        ctx = _metadata_context(a)
        assert ctx.name == "div"


# ── extract_article_links ─────────────────────────────────────────────────────

class TestExtractArticleLinks:
    def test_returns_four_tuples(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item", "https://example.com")
        assert all(len(item) == 4 for item in links)

    def test_direct_a_selector(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item h2 a", "https://example.com")
        assert len(links) == 3
        urls = [u for u, *_ in links]
        assert urls[0] == "https://example.com/news/1"
        assert urls[1] == "https://example.com/news/2"

    def test_container_selector_finds_inner_a(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item", "https://example.com")
        assert len(links) == 3
        urls = [u for u, *_ in links]
        assert "https://example.com/news/1" in urls

    def test_a_selector_uses_parent_for_published_at(self):
        """When selector returns <a>, published_at is taken from parent container."""
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item h2 a", "https://example.com")
        _, _, pub_at, _ = links[0]
        assert pub_at == datetime(2024, 3, 15, 10, 0, 0, tzinfo=timezone.utc)

    def test_a_selector_uses_parent_for_excerpt(self):
        """When selector returns <a>, excerpt is taken from parent container."""
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item h2 a", "https://example.com")
        _, _, _, excerpt = links[0]
        assert excerpt == "First article summary text here."

    def test_container_selector_extracts_published_at(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item", "https://example.com")
        _, _, pub_at, _ = links[0]
        assert pub_at == datetime(2024, 3, 15, 10, 0, 0, tzinfo=timezone.utc)

    def test_container_selector_extracts_excerpt(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item", "https://example.com")
        _, _, _, excerpt = links[0]
        assert excerpt == "First article summary text here."

    def test_no_time_tag_published_at_is_none(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item", "https://example.com")
        _, _, pub_at, _ = links[2]  # third article has no <time>
        assert pub_at is None

    def test_relative_urls_resolved(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "article.item h2 a", "https://example.com/news/")
        for url, *_ in links:
            assert url.startswith("https://example.com/")

    def test_absolute_urls_unchanged(self):
        html = '<html><body><a href="https://other.com/article">Title</a></body></html>'
        links = extract_article_links(html, "a", "https://example.com")
        assert links[0][0] == "https://other.com/article"

    def test_javascript_href_skipped(self):
        html = '<html><body><a href="javascript:void(0)">JS</a><a href="/real">Real</a></body></html>'
        links = extract_article_links(html, "a", "https://example.com")
        urls = [u for u, *_ in links]
        assert "https://example.com/real" in urls
        assert not any("javascript" in u for u in urls)

    def test_hash_href_skipped(self):
        html = '<html><body><a href="#">Anchor</a><a href="/real">Real</a></body></html>'
        links = extract_article_links(html, "a", "https://example.com")
        assert all(u != "https://example.com/#" for u, *_ in links)

    def test_no_match_returns_empty(self):
        links = extract_article_links(_HTML_WITH_ARTICLES, "div.nonexistent a", "https://example.com")
        assert links == []

    def test_duplicate_urls_deduplicated(self):
        html = '<html><body>' + '<a href="/art/1">Title</a>' * 5 + '</body></html>'
        links = extract_article_links(html, "a", "https://example.com")
        urls = [u for u, *_ in links]
        assert urls.count("https://example.com/art/1") == 1

    def test_title_from_heading_in_container(self):
        html = '<html><body><article><h2>My Heading</h2><a href="/x">link</a></article></body></html>'
        links = extract_article_links(html, "article", "https://example.com")
        assert links[0][1] == "My Heading"

    def test_title_from_link_text(self):
        html = '<html><body><article><a href="/x">My Article Title</a></article></body></html>'
        links = extract_article_links(html, "article a", "https://example.com")
        assert links[0][1] == "My Article Title"

    def test_empty_title_falls_back_to_url(self):
        html = '<html><body><a href="/x"> </a></body></html>'
        links = extract_article_links(html, "a", "https://example.com")
        assert links[0][1] == "https://example.com/x"

    def test_container_without_a_skipped(self):
        html = '<html><body><div class="card"><span>No link</span></div><div class="card"><a href="/ok">OK</a></div></body></html>'
        links = extract_article_links(html, "div.card", "https://example.com")
        assert len(links) == 1
        assert links[0][0] == "https://example.com/ok"

    def test_max_100_results(self):
        items = "".join(f'<a href="/a/{i}">Item {i}</a>' for i in range(200))
        html = f"<html><body>{items}</body></html>"
        links = extract_article_links(html, "a", "https://example.com")
        assert len(links) == 100


# ── generate_selector_prompt ──────────────────────────────────────────────────

class TestGenerateSelectorPrompt:
    def test_prompt_contains_url(self):
        prompt = generate_selector_prompt("https://news.example.com", _HTML_WITH_ARTICLES)
        assert "https://news.example.com" in prompt

    def test_prompt_contains_instruction(self):
        prompt = generate_selector_prompt("https://news.example.com", _HTML_WITH_ARTICLES)
        assert "CSS selector" in prompt
        assert "plain text" in prompt.lower() or "ONLY" in prompt

    def test_prompt_skips_nav_and_footer(self):
        prompt = generate_selector_prompt("https://news.example.com", _HTML_WITH_ARTICLES)
        assert "About" not in prompt or "article" in prompt.lower()

    def test_prompt_includes_article_content(self):
        prompt = generate_selector_prompt("https://news.example.com", _HTML_WITH_ARTICLES)
        assert "Article One" in prompt or "article" in prompt.lower()

    def test_prompt_skips_aria_hidden(self):
        """Elements with aria-hidden=true on themselves are excluded."""
        html = """<html><body>
            <div aria-hidden="true"><h2><a href="/x">Hidden</a></h2><p>Hidden content here.</p></div>
            <article><h2><a href="/y">Visible</a></h2><p>Real article excerpt here.</p></article>
        </body></html>"""
        prompt = generate_selector_prompt("https://example.com", html)
        assert "Hidden" not in prompt

    def test_prompt_skips_modal_class(self):
        html = """<html><body>
            <div class="modal"><h2><a href="/x">Modal Content</a></h2><p>Modal text here.</p></div>
            <article><h2><a href="/y">Article</a></h2><p>Real content here.</p></article>
        </body></html>"""
        prompt = generate_selector_prompt("https://example.com", html)
        assert "Modal Content" not in prompt

    def test_prompt_skips_large_blocks(self):
        """Blocks over 10000 chars should be excluded."""
        large_content = "word " * 3000
        html = f"""<html><body>
            <div><h2><a href="/x">Wrapper</a></h2><p>{large_content}</p></div>
            <article><h2><a href="/y">Article</a></h2><p>Short real content here.</p></article>
        </body></html>"""
        prompt = generate_selector_prompt("https://example.com", html)
        assert "Wrapper" not in prompt

    def test_prompt_with_empty_html(self):
        prompt = generate_selector_prompt("https://example.com", "<html><body></body></html>")
        assert "https://example.com" in prompt
        assert len(prompt) > 50

    def test_prompt_truncates_large_html(self):
        large_html = "<html><body>" + "<article><h2><a href='/x'>T</a></h2></article>" * 1000 + "</body></html>"
        prompt = generate_selector_prompt("https://example.com", large_html)
        assert len(prompt) < 10000


# ── fetch_scrape_feed — success path ─────────────────────────────────────────

class TestFetchScrapeFeedSuccess:
    async def test_returns_new_article_count(self):
        feed = _make_scrape_feed()
        session = _make_session()

        extract_readable_result = MagicMock()
        extract_readable_result.scalar.return_value = True

        session.execute = AsyncMock(side_effect=[
            MagicMock(scalars=MagicMock(return_value=MagicMock(__iter__=lambda s: iter([])))),  # guid_hash
            MagicMock(scalars=MagicMock(return_value=MagicMock(__iter__=lambda s: iter([])))),  # url_normalized
            extract_readable_result,  # extract_readable query
        ])
        with patch("app.fetcher.scrape.fetch_url_page", return_value=_page(_HTML_WITH_ARTICLES)), \
             patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()):
            count = await fetch_scrape_feed(feed, session)
        assert count == 3

    async def test_sets_feed_active_on_success(self):
        feed = _make_scrape_feed(
            status="error",
            fetch_error_count=2,
            retry_after_until=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", return_value=_page(_HTML_WITH_ARTICLES)), \
             patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()):
            await fetch_scrape_feed(feed, session)
        assert feed.status == "active"
        assert feed.fetch_error_count == 0
        assert feed.last_error is None
        assert feed.retry_after_until is None

    async def test_extract_readable_false_sets_skipped(self):
        """Articles get readable_status='skipped' when extract_readable is False."""
        feed = _make_scrape_feed()
        session = _make_session()

        extract_readable_result = MagicMock()
        extract_readable_result.scalar.return_value = False
        saved_articles = []

        original_add = session.add

        def capture_add(obj):
            from app.models.article import Article
            if isinstance(obj, Article):
                saved_articles.append(obj)
            original_add(obj)

        session.add = capture_add
        session.execute = AsyncMock(side_effect=[
            extract_readable_result,  # extract_readable query (before _save_scrape_articles)
            MagicMock(scalars=MagicMock(return_value=MagicMock(__iter__=lambda s: iter([])))),  # guid_hash
            MagicMock(scalars=MagicMock(return_value=MagicMock(__iter__=lambda s: iter([])))),  # url_normalized
        ])
        with patch("app.fetcher.scrape.fetch_url_page", return_value=_page(_HTML_WITH_ARTICLES)), \
             patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()):
            await fetch_scrape_feed(feed, session)

        assert all(a.readable_status == "skipped" for a in saved_articles)


# ── fetch_scrape_feed — error paths ──────────────────────────────────────────

class TestFetchScrapeFeedErrors:
    async def test_zero_links_triggers_error_state(self):
        feed = _make_scrape_feed(type_config={"article_links_selector": "div.nonexistent a"})
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", return_value=_page(_HTML_WITH_ARTICLES)):
            result = await fetch_scrape_feed(feed, session)
        assert result == 0
        session.execute.assert_called()

    async def test_zero_links_returns_zero(self):
        feed = _make_scrape_feed(type_config={"article_links_selector": "div.nothing"})
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", return_value=_page(_HTML_WITH_ARTICLES)):
            count = await fetch_scrape_feed(feed, session)
        assert count == 0

    async def test_network_error_returns_zero(self):
        feed = _make_scrape_feed()
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=ValueError("timeout")):
            result = await fetch_scrape_feed(feed, session)
        assert result == 0

    async def test_network_error_adds_fetchlog(self):
        feed = _make_scrape_feed()
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=ValueError("timeout")):
            await fetch_scrape_feed(feed, session)
        assert session.add.called
        added = session.add.call_args[0][0]
        assert isinstance(added, FetchLog)
        assert added.feed_id == 1
        assert "timeout" in added.error_message

    async def test_http_4xx_disables_feed(self):
        import httpx
        feed = _make_scrape_feed()
        session = _make_session()
        request = httpx.Request("GET", feed.feed_url)
        response = httpx.Response(404, request=request)
        exc = httpx.HTTPStatusError("404", request=request, response=response)
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=exc):
            await fetch_scrape_feed(feed, session)
        assert session.execute.called

    async def test_missing_type_config_returns_zero(self):
        feed = _make_scrape_feed(type_config=None)
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", return_value=_page(_HTML_WITH_ARTICLES)):
            result = await fetch_scrape_feed(feed, session)
        assert result == 0


def _scrape_http_error(status: int, headers: dict | None = None):
    import httpx
    request = httpx.Request("GET", "https://example.com/news")
    response = httpx.Response(status, request=request, headers=headers or {})
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _scrape_update_values(session) -> dict:
    stmt = session.execute.call_args[0][0]
    return {col.name: val for col, val in stmt._values.items()}


def _scrape_status_is_disabled(status_clause) -> bool:
    from sqlalchemy.sql.elements import BindParameter
    return isinstance(status_clause, BindParameter) and status_clause.value == "disabled"


class TestFetchScrapeFeed429Transient:
    """403/408/429 back the scrape feed off through the error tier instead of
    disabling it on the first hit; genuinely permanent 4xx still disable."""

    async def test_429_does_not_disable(self):
        feed = _make_scrape_feed()
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=_scrape_http_error(429)):
            await fetch_scrape_feed(feed, session)
        assert not _scrape_status_is_disabled(_scrape_update_values(session)["status"])

    async def test_429_sets_retry_after_until_from_header(self):
        # A 429 is a block: the feed always gets at least BLOCK_BACKOFF_BASE, and a
        # longer Retry-After from the host wins over it.
        feed = _make_scrape_feed()
        session = _make_session()
        exc = _scrape_http_error(429, headers={"Retry-After": "3600"})
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=exc):
            await fetch_scrape_feed(feed, session)
        rau = _scrape_update_values(session)["retry_after_until"].value
        assert rau is not None
        assert before + timedelta(seconds=3590) <= rau <= before + timedelta(seconds=3610)

    async def test_403_does_not_disable_on_first_hit(self):
        feed = _make_scrape_feed()
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=_scrape_http_error(403)):
            await fetch_scrape_feed(feed, session)
        assert not _scrape_status_is_disabled(_scrape_update_values(session)["status"])

    async def test_403_backs_off_on_the_block_tier(self):
        # A bare 403 arms the block backoff rather than the feed's error tier, so the
        # scheduler stops retrying a refusing host on the normal interval.
        feed = _make_scrape_feed()
        session = _make_session()
        before = datetime.now(timezone.utc)
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=_scrape_http_error(403)):
            await fetch_scrape_feed(feed, session)
        vals = _scrape_update_values(session)
        rau = vals["retry_after_until"].value
        assert abs((rau - (before + BLOCK_BACKOFF_BASE)).total_seconds()) < 5
        assert "fetch_error_count" not in vals

    async def test_permanent_4xx_still_disables(self):
        feed = _make_scrape_feed()
        session = _make_session()
        with patch("app.fetcher.scrape.fetch_url_page", side_effect=_scrape_http_error(404)):
            await fetch_scrape_feed(feed, session)
        assert _scrape_status_is_disabled(_scrape_update_values(session)["status"])


# ── subscribe_scrape service ──────────────────────────────────────────────────

class TestSubscribeScrape:
    def _make_db(self, existing_feed=None, existing_subscription=None,
                 feed_count=0, max_feeds=200):
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()

        call_count = 0

        async def execute_side_effect(stmt, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = max_feeds
            elif call_count == 2:
                result.scalar.return_value = feed_count
            elif call_count == 3:
                result.scalar_one_or_none.return_value = existing_feed
            elif call_count == 4 and existing_feed:
                result.scalar_one_or_none.return_value = existing_subscription
            else:
                result.scalar_one_or_none.return_value = None
            return result

        db.execute = AsyncMock(side_effect=execute_side_effect)
        return db

    async def test_creates_new_feed_and_user_feed(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        db = self._make_db(existing_feed=None, feed_count=0)

        def _close_coro(coro):
            coro.close()

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()), \
             patch("app.fetcher.scrape.fetch_page_html", new=AsyncMock(return_value=_HTML_WITH_ARTICLES)), \
             patch("app.services.feed.asyncio.create_task", side_effect=_close_coro):
            uf = await subscribe_scrape(
                user=user, url="https://example.com/news",
                selector="article a", title="Example News",
                folder_id=None, db=db,
            )
        assert db.add.called
        assert db.commit.called

    async def test_reuses_existing_feed(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        existing = SimpleNamespace(id=42, subscriber_count=3)
        db = self._make_db(existing_feed=existing, existing_subscription=None, feed_count=1)

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()), \
             patch("app.fetcher.scrape.fetch_page_html", new=AsyncMock(return_value=_HTML_WITH_ARTICLES)):
            await subscribe_scrape(
                user=user, url="https://example.com/news",
                selector="article a", title="Example News",
                folder_id=None, db=db,
            )
        added_types = [type(c[0][0]).__name__ for c in db.add.call_args_list]
        assert "UserFeed" in added_types
        assert "Feed" not in added_types

    async def test_already_subscribed_raises(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        existing = SimpleNamespace(id=42, subscriber_count=1)
        existing_sub = SimpleNamespace(id=99)
        db = self._make_db(existing_feed=existing, existing_subscription=existing_sub, feed_count=1)

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()), \
             patch("app.fetcher.scrape.fetch_page_html", new=AsyncMock(return_value=_HTML_WITH_ARTICLES)):
            with pytest.raises(ValueError, match="Already subscribed"):
                await subscribe_scrape(
                    user=user, url="https://example.com/news",
                    selector="article a", title="News",
                    folder_id=None, db=db,
                )

    async def test_empty_selector_raises(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        db = self._make_db(feed_count=0)

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()):
            with pytest.raises(ValueError, match="selector"):
                await subscribe_scrape(
                    user=user, url="https://example.com",
                    selector="   ", title="News",
                    folder_id=None, db=db,
                )

    async def test_selector_too_long_raises(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        db = self._make_db(feed_count=0)

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()):
            with pytest.raises(ValueError, match="too long"):
                await subscribe_scrape(
                    user=user, url="https://example.com",
                    selector="a" * 501, title="News",
                    folder_id=None, db=db,
                )

    async def test_feed_limit_raises(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        db = self._make_db(feed_count=200, max_feeds=200)

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()):
            with pytest.raises(ValueError, match="Feed limit"):
                await subscribe_scrape(
                    user=user, url="https://example.com",
                    selector="article a", title="News",
                    folder_id=None, db=db,
                )

    async def test_preflight_no_links_raises(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        db = self._make_db(feed_count=0)
        empty_html = "<html><body><nav><a href='/about'>About</a></nav></body></html>"

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()), \
             patch("app.fetcher.scrape.fetch_page_html", new=AsyncMock(return_value=empty_html)):
            with pytest.raises(ValueError, match="matched no article links"):
                await subscribe_scrape(
                    user=user, url="https://example.com",
                    selector="article a", title="News",
                    folder_id=None, db=db,
                )

    async def test_preflight_fetch_error_raises(self):
        from tests.conftest import make_mock_user
        from app.services.feed import subscribe_scrape

        user = make_mock_user()
        db = self._make_db(feed_count=0)

        with patch("app.services.feed.async_validate_feed_url", new=AsyncMock()), \
             patch("app.fetcher.scrape.fetch_page_html", new=AsyncMock(side_effect=Exception("timeout"))):
            with pytest.raises(ValueError, match="Could not fetch the page"):
                await subscribe_scrape(
                    user=user, url="https://example.com",
                    selector="article a", title="News",
                    folder_id=None, db=db,
                )


# ── _save_scrape_articles: retention cutoff (#17) ─────────────────────────────

class TestSaveScrapeCutoff:
    def _links(self):
        return [
            ("https://example.com/old", "Old", datetime(2024, 2, 1, tzinfo=timezone.utc), None),
            ("https://example.com/new", "New", datetime(2024, 3, 10, tzinfo=timezone.utc), None),
            ("https://example.com/nodate", "NoDate", None, None),
        ]

    async def test_skips_dated_links_before_cutoff(self):
        from app.fetcher.scrape import _save_scrape_articles
        session = _make_session()
        feed = _make_scrape_feed()
        cutoff = datetime(2024, 3, 1, tzinfo=timezone.utc)
        fetched_at = datetime(2024, 3, 20, tzinfo=timezone.utc)

        with patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()):
            count = await _save_scrape_articles(feed, self._links(), fetched_at, session, cutoff)

        # Old (before cutoff) skipped; New (after) and NoDate (undated) kept.
        assert count == 2
        added_urls = {c.args[0].url for c in session.add.call_args_list}
        assert added_urls == {"https://example.com/new", "https://example.com/nodate"}

    async def test_no_cutoff_keeps_all(self):
        from app.fetcher.scrape import _save_scrape_articles
        session = _make_session()
        feed = _make_scrape_feed()
        fetched_at = datetime(2024, 3, 20, tzinfo=timezone.utc)

        with patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()):
            count = await _save_scrape_articles(feed, self._links(), fetched_at, session, None)

        assert count == 3

    async def test_undated_link_stores_null_published_at(self):
        # Undated listings must store published_at=None (not fetched_at) so readable
        # extraction can backfill the real date via htmldate; dated links keep theirs.
        from app.fetcher.scrape import _save_scrape_articles
        session = _make_session()
        feed = _make_scrape_feed()
        fetched_at = datetime(2024, 3, 20, tzinfo=timezone.utc)

        with patch("app.services.filter_service.apply_filters_to_new_articles", new=AsyncMock()):
            await _save_scrape_articles(feed, self._links(), fetched_at, session, None)

        added = {c.args[0].url: c.args[0] for c in session.add.call_args_list}
        assert added["https://example.com/nodate"].published_at is None
        assert added["https://example.com/new"].published_at == datetime(2024, 3, 10, tzinfo=timezone.utc)


class TestScrapeTargetUrl:
    """The scrape helpers address an existing feed by id and read its URL from the
    database, so the stored address never has to sit in a hidden form field: it can
    carry an API token in the query. Credentials that used to live in the address are
    read from the feed's own columns alongside it."""

    def _db(self, url_value, auth_user=None, auth_pass_enc=None):
        db = AsyncMock()
        recorded = {}
        row = None if url_value is None else SimpleNamespace(
            feed_url=url_value,
            fetch_auth_user=auth_user,
            fetch_auth_pass_encrypted=auth_pass_enc,
        )

        async def _execute(stmt, *a, **kw):
            recorded["sql"] = str(stmt)
            result = MagicMock()
            result.first.return_value = row
            return result

        db.execute = AsyncMock(side_effect=_execute)
        return db, recorded

    async def test_feed_id_resolves_the_stored_url(self):
        from app.routers.web.settings.common import _scrape_target
        from tests.conftest import make_mock_user

        db, _ = self._db("https://example.com/news?api_key=secret")
        target = await _scrape_target({"feed_id": "7"}, make_mock_user(), db)

        assert target.url == "https://example.com/news?api_key=secret"
        assert target.auth is None

    async def test_stored_credentials_come_back_with_the_url(self):
        from app.routers.web.settings.common import _scrape_target
        from app.utils.crypto import encrypt
        from tests.conftest import make_mock_user

        db, _ = self._db("https://example.com/news", "bob", encrypt("hunter2"))
        target = await _scrape_target({"feed_id": "7"}, make_mock_user(), db)

        assert target.auth == ("bob", "hunter2")

    async def test_lookup_is_scoped_to_the_users_subscriptions(self):
        from app.routers.web.settings.common import _scrape_target
        from tests.conftest import make_mock_user

        db, recorded = self._db("https://example.com/news")
        await _scrape_target({"feed_id": "7"}, make_mock_user(role="user"), db)

        assert "user_feeds" in recorded["sql"]

    async def test_admin_lookup_is_not_scoped(self):
        from app.routers.web.settings.common import _scrape_target
        from tests.conftest import make_mock_user

        db, recorded = self._db("https://example.com/news")
        await _scrape_target({"feed_id": "7"}, make_mock_user(role="admin"), db)

        assert "user_feeds" not in recorded["sql"]

    async def test_feed_the_user_cannot_reach_yields_no_url(self):
        # Scoped-out row or unknown id: the caller reports it like a missing URL
        # rather than fetching something the user has no claim to.
        from app.routers.web.settings.common import _scrape_target
        from tests.conftest import make_mock_user

        db, _ = self._db(None)
        assert (await _scrape_target({"feed_id": "7"}, make_mock_user(), db)).url == ""

    async def test_setup_flow_still_uses_the_typed_url(self):
        from app.routers.web.settings.common import _scrape_target
        from tests.conftest import make_mock_user

        db, _ = self._db(None)
        target = await _scrape_target({"url": "example.com/news"}, make_mock_user(), db)

        assert target.url == "https://example.com/news"
        assert not db.execute.called

    async def test_setup_flow_splits_credentials_out_of_the_typed_url(self):
        # No feed row exists yet, so the address is the only place the credentials
        # can come from, and the preview has to be able to get past the login.
        from app.routers.web.settings.common import _scrape_target
        from tests.conftest import make_mock_user

        db, _ = self._db(None)
        target = await _scrape_target(
            {"url": "https://bob:hunter2@example.com/news"}, make_mock_user(), db
        )

        assert target == ("https://example.com/news", ("bob", "hunter2"))


class TestScrapePreviewEndpoint:
    URL = "/settings/feeds/scrape-preview"

    def _row(self, mock_db, url_value):
        row = None if url_value is None else SimpleNamespace(
            feed_url=url_value, fetch_auth_user=None, fetch_auth_pass_encrypted=None
        )
        result = MagicMock()
        result.first.return_value = row
        mock_db.execute = AsyncMock(return_value=result)

    def test_foreign_feed_id_is_not_fetched(self, client, mock_db):
        # The id is the only thing the edit form sends, so it has to be checked:
        # otherwise anyone could preview (and thereby fetch) another user's feed.
        self._row(mock_db, None)
        with patch("app.routers.web.settings.scrape.fetch_page_html",
                   new=AsyncMock()) as fetch:
            resp = client.post(self.URL, data={"feed_id": "7", "selector": "article a"})

        assert resp.status_code == 200
        assert "required" in resp.text.lower()
        assert not fetch.called

    def test_subscribed_feed_is_fetched_by_id(self, client, mock_db):
        self._row(mock_db, "https://example.com/news?api_key=s3cret")
        with patch("app.routers.web.settings.scrape.fetch_page_html",
                   new=AsyncMock(return_value=_HTML_WITH_ARTICLES)) as fetch:
            resp = client.post(self.URL, data={"feed_id": "7", "selector": "article a"})

        assert resp.status_code == 200
        assert fetch.await_args.args[0] == "https://example.com/news?api_key=s3cret"

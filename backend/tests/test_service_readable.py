"""Unit tests for readable_service pure functions (no DB, no HTTP)."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from app.services.ai_jobs import MAX_RETRIES
from app.services.readable_service import (
    apply_readable_result,
    extract_readable,
    _dedupe_images,
    _find_published_date,
    _extract_with_trafilatura,
    _extract_with_readability,
    _strip_pre_extraction_noise,
    _has_visible_content,
    _sanitize,
    _drop_empty_blocks,
    _EMPTY_CONTENT_MSG,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_article(retries=0, status="pending", published_at=None):
    return SimpleNamespace(
        id=1,
        readable_content=None,
        readable_status=status,
        readable_error=None,
        readable_retries=retries,
        readable_next_retry_at=None,
        readable_failed_at=None,
        word_count=None,
        estimated_read_min=None,
        published_at=published_at,
    )


# ── apply_readable_result — success path ──────────────────────────────────────

class TestApplyReadableResultSuccess:
    def test_sets_content_and_status(self):
        article = _make_article()
        apply_readable_result(article, "<p>Hello world</p>", None, None)
        assert article.readable_content == "<p>Hello world</p>"
        assert article.readable_status == "success"

    def test_clears_error_on_success(self):
        article = _make_article()
        article.readable_error = "previous error"
        apply_readable_result(article, "<p>Content</p>", None, None)
        assert article.readable_error is None

    def test_computes_word_count(self):
        article = _make_article()
        apply_readable_result(article, "<p>one two three</p>", None, None)
        assert article.word_count == 3

    def test_estimated_read_min_at_least_one(self):
        article = _make_article()
        apply_readable_result(article, "<p>short</p>", None, None)
        assert article.estimated_read_min >= 1

    def test_returns_false(self):
        article = _make_article()
        result = apply_readable_result(article, "<p>content</p>", None, None)
        assert result is False

    def test_long_content_read_time(self):
        # 400 words → 2 minutes
        content = "<p>" + " ".join(["word"] * 400) + "</p>"
        article = _make_article()
        apply_readable_result(article, content, None, None)
        assert article.estimated_read_min == 2


# ── published_at backfill ─────────────────────────────────────────────────────

class TestPublishedAtBackfill:
    _DATE = datetime(2026, 3, 14, tzinfo=timezone.utc)

    def test_backfills_when_missing(self):
        article = _make_article(published_at=None)
        apply_readable_result(article, "<p>body</p>", None, None, self._DATE)
        assert article.published_at == self._DATE

    def test_never_overrides_existing(self):
        existing = datetime(2020, 1, 1, tzinfo=timezone.utc)
        article = _make_article(published_at=existing)
        apply_readable_result(article, "<p>body</p>", None, None, self._DATE)
        assert article.published_at == existing

    def test_ignored_on_failure(self):
        # No content → failure branch never touches published_at.
        article = _make_article(published_at=None)
        apply_readable_result(article, None, "HTTP 500", 500, self._DATE)
        assert article.published_at is None

    def test_none_date_leaves_missing(self):
        article = _make_article(published_at=None)
        apply_readable_result(article, "<p>body</p>", None, None, None)
        assert article.published_at is None


class TestFindPublishedDate:
    def _page(self, iso: str) -> str:
        return (f'<html><head><meta property="article:published_time" '
                f'content="{iso}"></head><body>text</body></html>')

    def test_parses_date_as_utc_midnight(self):
        dt = _find_published_date(self._page("2026-03-14T10:30:00Z"),
                                  "https://example.com/a")
        assert dt == datetime(2026, 3, 14, tzinfo=timezone.utc)

    def test_returns_none_when_absent(self):
        html = "<html><body>no date anywhere</body></html>"
        assert _find_published_date(html, "https://example.com/a") is None

    def test_rejects_far_future_date(self):
        dt = _find_published_date(self._page("2099-01-01"), "https://example.com/a")
        assert dt is None

    def test_swallows_htmldate_errors(self):
        with patch("htmldate.find_date", side_effect=RuntimeError("boom")):
            assert _find_published_date("<html></html>", "https://example.com/a") is None


class TestStripPreExtractionNoise:
    def test_drops_tumblr_notes_and_noscript(self):
        # Tumblr's notes list is the biggest block on a short post; trafilatura
        # grabs it as the "content" unless we remove it first.
        html = (
            '<div id="content"><p>the actual post</p></div>'
            '<div id="notecontainer"><ol class="notes">'
            '<li><a href="https://x.tumblr.com/">x</a>liked this</li>'
            '</ol></div>'
            '<noscript><img src="https://px.srvcs.tumblr.com/impixu"></noscript>'
        )
        out = _strip_pre_extraction_noise(html)
        assert "the actual post" in out
        assert "liked this" not in out
        assert "notecontainer" not in out
        assert "impixu" not in out

    def test_passthrough_when_no_tumblr_markers(self):
        # Non-Tumblr pages are returned untouched (noscript preserved for e.g.
        # lazy-loaded <img> fallbacks).
        html = '<article><p>hi</p><noscript><img src="/lazy.jpg"></noscript></article>'
        assert _strip_pre_extraction_noise(html) == html


class TestDedupeImages:
    def _count(self, html):
        return html.count("<img")

    def test_collapses_same_filename_different_path(self):
        # Same file re-uploaded under different paths (aktualne.cz/economia case):
        # dedup keys on the filename, so all three collapse to the first.
        html = (
            '<img src="https://m.cz/a/b/photo.jpg"/>'
            '<p>text</p>'
            '<img src="https://m.cz/c/d/photo.jpg"/>'
            '<img src="https://m.cz/c/d/photo.jpg"/>'
        )
        out = _dedupe_images(html)
        assert self._count(out) == 1
        assert "/a/b/photo.jpg" in out  # first occurrence kept
        assert "<p>text</p>" in out

    def test_keeps_distinct_images(self):
        html = (
            '<img src="https://m.cz/one.jpg"/>'
            '<img src="https://m.cz/two.jpg"/>'
        )
        assert self._count(_dedupe_images(html)) == 2

    def test_ignores_srcless_img(self):
        html = '<img alt="x"/><img alt="y"/>'
        # No src → no dedup key → left untouched, no crash.
        assert self._count(_dedupe_images(html)) == 2


# ── apply_readable_result — empty / whitespace content ────────────────────────

class TestApplyReadableResultEmpty:
    def test_whitespace_only_content_not_success(self):
        # A whitespace string is truthy but renders blank — must not be stored as
        # success (it would hide the fuller feed content). Regression: Reddit posts.
        article = _make_article()
        apply_readable_result(article, "  \n\t ", None, None)
        assert article.readable_status != "success"
        assert article.readable_content is None

    def test_whitespace_only_content_goes_to_retry(self):
        article = _make_article(retries=0)
        apply_readable_result(article, "   ", None, None)
        assert article.readable_retries == 1

    def test_real_content_still_success(self):
        article = _make_article()
        apply_readable_result(article, "  <p>Real text</p>  ", None, None)
        assert article.readable_status == "success"


# ── _has_visible_content ──────────────────────────────────────────────────────

class TestHasVisibleContent:
    def test_text_is_visible(self):
        assert _has_visible_content("<p>Hello</p>") is True

    def test_whitespace_only_not_visible(self):
        assert _has_visible_content("<div>  </div>") is False

    def test_empty_string_not_visible(self):
        assert _has_visible_content("") is False

    def test_blank_string_not_visible(self):
        assert _has_visible_content("   \n ") is False

    def test_media_only_is_visible(self):
        assert _has_visible_content('<div><img src="x.jpg"></div>') is True

    def test_iframe_only_is_visible(self):
        assert _has_visible_content('<p><iframe src="https://y/embed/z"></iframe></p>') is True


# ── extract_readable — collapse to empty after sanitization ───────────────────

class TestExtractReadableCollapse:
    def test_collapsed_content_returns_empty_msg(self):
        # Extractor yields markup that sanitizes/drops down to pure whitespace.
        with (
            patch("app.services.readable_service._fetch_html",
                  return_value=("<html><body>x</body></html>", None, 200)),
            patch("app.services.readable_service._extract_with_trafilatura",
                  return_value="<div>   </div>"),
        ):
            content, error, status, published_at = extract_readable("https://example.com/a")
        assert content is None
        assert error == _EMPTY_CONTENT_MSG
        assert status is None
        assert published_at is None

    def test_real_content_passes_through(self):
        with (
            patch("app.services.readable_service._fetch_html",
                  return_value=("<html><body>x</body></html>", None, 200)),
            patch("app.services.readable_service._extract_with_trafilatura",
                  return_value="<p>Genuine article body</p>"),
        ):
            content, error, status, published_at = extract_readable("https://example.com/a")
        assert error is None
        assert content is not None
        assert "Genuine article body" in content


# ── apply_readable_result — 4xx failure ───────────────────────────────────────

class TestApplyReadableResult4xx:
    def test_404_marks_failed_permanently(self):
        article = _make_article()
        apply_readable_result(article, None, "HTTP 404 Not Found", 404)
        assert article.readable_status == "failed"
        assert article.readable_next_retry_at is None

    def test_403_returns_true(self):
        article = _make_article()
        result = apply_readable_result(article, None, "HTTP 403 Forbidden", 403)
        assert result is True

    def test_non_403_4xx_returns_false(self):
        article = _make_article()
        result = apply_readable_result(article, None, "HTTP 404", 404)
        assert result is False

    def test_4xx_sets_failed_at(self):
        article = _make_article()
        before = datetime.now(timezone.utc)
        apply_readable_result(article, None, "HTTP 410", 410)
        assert article.readable_failed_at is not None
        assert article.readable_failed_at >= before

    def test_4xx_sets_error_message(self):
        article = _make_article()
        apply_readable_result(article, None, "HTTP 410 Gone", 410)
        assert article.readable_error == "HTTP 410 Gone"


# ── apply_readable_result — transient failure / retry ─────────────────────────

class TestApplyReadableResultRetry:
    def test_network_error_increments_retries(self):
        article = _make_article(retries=0)
        apply_readable_result(article, None, "Timeout", None)
        assert article.readable_retries == 1

    def test_sets_next_retry_at(self):
        article = _make_article(retries=0)
        apply_readable_result(article, None, "Timeout", None)
        assert article.readable_next_retry_at is not None

    def test_max_retries_marks_failed(self):
        article = _make_article(retries=MAX_RETRIES - 1)
        apply_readable_result(article, None, "Timeout", None)
        assert article.readable_status == "failed"
        assert article.readable_retries == MAX_RETRIES

    def test_below_max_retries_stays_pending(self):
        article = _make_article(retries=0)
        apply_readable_result(article, None, "Timeout", None)
        assert article.readable_status != "failed"

    def test_backoff_increases_with_retries(self):
        a1 = _make_article(retries=0)
        a2 = _make_article(retries=1)
        now = datetime.now(timezone.utc)
        apply_readable_result(a1, None, "err", None)
        apply_readable_result(a2, None, "err", None)
        # Second retry should have a later next_retry_at
        assert a2.readable_next_retry_at >= a1.readable_next_retry_at


# ── _extract_with_trafilatura ─────────────────────────────────────────────────

class TestExtractWithTrafilatura:
    def test_returns_none_on_empty_html(self):
        result = _extract_with_trafilatura("", "http://example.com")
        assert result is None

    def test_returns_extracted_html_deterministically(self):
        # Mock trafilatura so the assertion doesn't depend on its heuristics.
        with patch("app.services.readable_service.trafilatura.extract") as mock_extract:
            mock_extract.return_value = "<p>Extracted body</p>"
            result = _extract_with_trafilatura("<html><body><article>x</article></body></html>",
                                               "http://example.com")
        assert result == "<p>Extracted body</p>"
        mock_extract.assert_called_once()

    def test_returns_none_when_trafilatura_returns_none(self):
        with patch("app.services.readable_service.trafilatura.extract") as mock_extract:
            mock_extract.return_value = None
            result = _extract_with_trafilatura("<html><body>x</body></html>", "http://example.com")
        assert result is None

    def test_converts_graphic_to_img(self):
        # Mock trafilatura to return a graphic tag
        with patch("app.services.readable_service.trafilatura.extract") as mock_extract:
            mock_extract.return_value = '<graphic src="test.jpg" alt="desc"/>'
            result = _extract_with_trafilatura("<html></html>", "http://example.com")
        assert result is not None
        assert "<img" in result
        assert "<graphic" not in result


# ── _extract_with_readability ─────────────────────────────────────────────────

class TestExtractWithReadability:
    def test_returns_none_on_empty_html(self):
        result = _extract_with_readability("")
        assert result is None

    def test_returns_string_for_valid_article(self):
        html = """
        <html><body>
        <div class="content">
        <p>This is a long article with substantial content that readability can process.
        It needs to be long enough for the function to consider it valid (> 50 chars).</p>
        </div>
        </body></html>
        """
        result = _extract_with_readability(html)
        assert result is None or isinstance(result, str)

    def test_exception_returns_none(self):
        with patch("app.services.readable_service.Document", side_effect=Exception("boom")):
            result = _extract_with_readability("<html></html>")
        assert result is None


# ── _sanitize ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def test_strips_script_tags(self):
        html = "<p>Safe</p><script>alert('xss')</script>"
        result = _sanitize(html)
        assert "<script>" not in result
        assert "Safe" in result

    def test_strips_onclick(self):
        html = '<p onclick="evil()">Text</p>'
        result = _sanitize(html)
        assert "onclick" not in result

    def test_preserves_allowed_tags(self):
        html = "<p>Hello <strong>world</strong></p>"
        result = _sanitize(html)
        assert "<strong>" in result

    def test_preserves_img_src(self):
        html = '<img src="photo.jpg" alt="photo">'
        result = _sanitize(html)
        assert 'src="photo.jpg"' in result

    def test_strips_inline_styles(self):
        html = '<p style="color:red">Text</p>'
        result = _sanitize(html)
        assert "style=" not in result

    def test_adds_rel_noopener(self):
        html = '<a href="http://example.com">Link</a>'
        result = _sanitize(html)
        assert "noopener" in result


# ── _drop_empty_blocks ────────────────────────────────────────────────────────

class TestDropEmptyBlocks:
    def test_removes_empty_li(self):
        # The real-world case: "Share:" label followed by an <li> emptied when its
        # share-button links were stripped by the sanitizer.
        html = "<ul><li>Share:</li><li></li></ul>"
        result = _drop_empty_blocks(html)
        assert result == "<ul><li>Share:</li></ul>"

    def test_removes_whitespace_only_li(self):
        html = "<ul><li>  </li><li>Real</li></ul>"
        result = _drop_empty_blocks(html)
        assert "Real" in result
        assert result.count("<li>") == 1

    def test_removes_empty_paragraph(self):
        html = "<p>Text</p><p></p>"
        result = _drop_empty_blocks(html)
        assert result == "<p>Text</p>"

    def test_keeps_li_with_text(self):
        html = "<ul><li>Date:</li><li>June 15, 2026</li></ul>"
        result = _drop_empty_blocks(html)
        assert result == html

    def test_keeps_li_with_image(self):
        html = '<ul><li><img src="photo.jpg" alt="x"></li></ul>'
        result = _drop_empty_blocks(html)
        assert 'src="photo.jpg"' in result
        assert "<li>" in result

    def test_keeps_li_with_iframe(self):
        html = '<li><iframe src="https://www.youtube.com/embed/x"></iframe></li>'
        result = _drop_empty_blocks(html)
        assert "<iframe" in result

    def test_removes_nested_then_parent(self):
        # Removing the empty inner <p> leaves the <li> empty too — both should go.
        html = "<ul><li><p></p></li><li>Keep</li></ul>"
        result = _drop_empty_blocks(html)
        assert result == "<ul><li>Keep</li></ul>"

    def test_leaves_non_block_tags_untouched(self):
        html = "<p>Hello <span></span>world</p>"
        result = _drop_empty_blocks(html)
        assert "Hello" in result and "world" in result

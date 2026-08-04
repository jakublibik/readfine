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
                  return_value=("<html><body>x</body></html>", None, 200, "https://example.com/a")),
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
                  return_value=("<html><body>x</body></html>", None, 200, "https://example.com/a")),
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


# ── consent/paywall substitute pages ─────────────────────────────────────────

class TestContentContradictsPage:
    """Some sites answer a server-side fetch with HTTP 200 and a consent page in
    place of the article. Status, length and extraction all look fine, so the only
    thing that gives it away is that the text has nothing to do with the page's own
    og:description (which on a real article is the lede)."""

    from app.services.readable_service import _content_contradicts_page as _check

    LEDE = ("Czech Hydrometeorological Institute declared a smog situation for the "
            "whole Usti region and Prague because of high ground level ozone "
            "concentrations, warning seniors and children about physical exertion")

    def test_consent_wall_is_rejected(self):
        from app.services.readable_service import _content_contradicts_page
        consent = ("<p>If you consent to advertising cookies and other network "
                   "identifiers for targeted advertising purposes, our partners will "
                   "display personalised commercial messages based on profiling.</p>")
        assert _content_contradicts_page(consent, self.LEDE) is True

    def test_real_article_body_passes(self):
        from app.services.readable_service import _content_contradicts_page
        body = "<p>" + self.LEDE + ". The institute added further detail.</p>"
        assert _content_contradicts_page(body, self.LEDE) is False

    def test_partial_overlap_still_passes(self):
        """The threshold sits far below the worst legitimate case measured (0.55),
        so a lede that is only loosely echoed must not be flagged."""
        from app.services.readable_service import _content_contradicts_page
        body = ("<p>Czech Hydrometeorological Institute warned about ozone "
                "concentrations and physical exertion outdoors today.</p>")
        assert _content_contradicts_page(body, self.LEDE) is False

    def test_short_description_skips_the_check(self):
        """Under the word floor the score is noise, so the check must abstain rather
        than guess — abstaining means behaving exactly as before."""
        from app.services.readable_service import _content_contradicts_page
        assert _content_contradicts_page("<p>totally unrelated text here</p>",
                                         "Latest news") is False

    def test_missing_description_skips_the_check(self):
        from app.services.readable_service import _content_contradicts_page
        assert _content_contradicts_page("<p>anything at all</p>", None) is False

    def test_empty_body_is_not_flagged_here(self):
        """Empty content is _EMPTY_CONTENT_MSG's job, handled before this runs."""
        from app.services.readable_service import _content_contradicts_page
        assert _content_contradicts_page("", self.LEDE) is False


class TestRejectWrongContentIsOptIn:
    def test_off_by_default(self):
        """Feed articles must never lose a body they have been showing fine."""
        from app.services.readable_service import extract_readable_with_title
        consent = "<html><body><p>" + ("consent advertising cookies partners " * 20) + "</p></body></html>"
        head = '<meta property="og:description" content="Institute declared smog situation because ground level ozone concentrations warned seniors children physical exertion outdoors">'
        page = "<html><head>" + head + "</head><body>" + consent + "</body></html>"
        with patch("app.services.readable_service._fetch_html",
                   return_value=(page, None, None, "https://x.invalid/a")):
            r = extract_readable_with_title("https://x.invalid/a")
        assert r.content is not None
        assert r.error is None

    def test_on_when_requested(self):
        from app.services.readable_service import extract_readable_with_title, _WRONG_CONTENT_MSG
        consent = "<p>" + ("consent advertising cookies partners profiling " * 20) + "</p>"
        head = '<meta property="og:description" content="Institute declared smog situation because ground level ozone concentrations warned seniors children physical exertion outdoors">'
        page = "<html><head>" + head + "</head><body>" + consent + "</body></html>"
        with patch("app.services.readable_service._fetch_html",
                   return_value=(page, None, None, "https://x.invalid/a")):
            r = extract_readable_with_title(
                "https://x.invalid/a", None, None, True
            )
        assert r.content is None
        assert r.error == _WRONG_CONTENT_MSG


# ── resolving the address an article really lives at ─────────────────────────

class TestResolveArticleUrl:
    """What gets pasted is often a click tracker or carries campaign parameters, so
    the address the fetch ended at (refined by the page's own canonical) is what
    should be stored."""

    CANON = '<link rel="canonical" href="https://www.idnes.cz/zpravy/story">'
    OGURL = '<meta property="og:url" content="https://www.idnes.cz/zpravy/story">'

    def test_prefers_same_host_canonical(self):
        from app.services.readable_service import resolve_article_url
        out = resolve_article_url(
            "https://www.idnes.cz/zpravy/story?utm_source=rss",
            "<html><head>" + self.CANON + "</head></html>",
        )
        assert out == "https://www.idnes.cz/zpravy/story"

    def test_falls_back_to_og_url(self):
        from app.services.readable_service import resolve_article_url
        out = resolve_article_url(
            "https://www.idnes.cz/zpravy/story?x=1",
            "<html><head>" + self.OGURL + "</head></html>",
        )
        assert out == "https://www.idnes.cz/zpravy/story"

    def test_cross_host_canonical_is_ignored(self):
        """A syndicated article naming the original publisher as canonical must not
        drag this article's URL onto another site — that would let dedup attach the
        save to an entirely different article."""
        from app.services.readable_service import resolve_article_url
        page = '<html><head><link rel="canonical" href="https://origin.example/other"></head></html>'
        out = resolve_article_url("https://syndicator.example/copy", page)
        assert out == "https://syndicator.example/copy"

    def test_relative_canonical_is_ignored(self):
        from app.services.readable_service import resolve_article_url
        page = '<html><head><link rel="canonical" href="/zpravy/story"></head></html>'
        out = resolve_article_url("https://www.idnes.cz/a", page)
        assert out == "https://www.idnes.cz/a"

    def test_no_canonical_keeps_the_fetched_url(self):
        from app.services.readable_service import resolve_article_url
        assert resolve_article_url("https://ex.invalid/a", "<html></html>") == "https://ex.invalid/a"

    def test_nothing_fetched(self):
        from app.services.readable_service import resolve_article_url
        assert resolve_article_url(None, "<html></html>") is None


# ── how far in the metadata is looked for ────────────────────────────────────

class TestHeadSlice:
    """The metadata regexes are bounded by </head>, not by a byte count.

    A fixed prefix used to stand in for the head, which lost the metadata of any page
    that opens with a large inline script block. YouTube is the live example: title,
    og: tags and rel=canonical all sit past 680 KB. Losing the description there also
    disabled _content_contradicts_page, so a page of footer links was stored as the
    article body.
    """

    # Comfortably past the old 200 KB window, comfortably inside the 1 MB cap.
    PADDING = "<script>var x = '%s';</script>" % ("a" * 400_000)

    def _page(self, meta: str) -> str:
        return f"<html><head>{self.PADDING}{meta}</head><body>text</body></html>"

    def test_title_past_the_old_window(self):
        from app.services.readable_service import _extract_title
        page = self._page('<meta property="og:title" content="Real title">')
        assert _extract_title(page) == "Real title"

    def test_description_past_the_old_window(self):
        from app.services.readable_service import _extract_og_description
        page = self._page('<meta property="og:description" content="The lede.">')
        assert _extract_og_description(page) == "The lede."

    def test_canonical_past_the_old_window(self):
        from app.services.readable_service import resolve_article_url
        page = self._page('<link rel="canonical" href="https://ex.invalid/story">')
        assert resolve_article_url("https://ex.invalid/story?utm=1", page) == \
            "https://ex.invalid/story"

    def test_body_is_not_scanned(self):
        """Whatever an article's own text contains, it is not the page's metadata."""
        from app.services.readable_service import _extract_title
        page = ('<html><head><title>Head title</title></head><body>'
                '<meta property="og:title" content="Body title"></body></html>')
        assert _extract_title(page) == "Head title"

    def test_no_closing_tag_falls_back_to_a_prefix(self):
        """Broken markup or a non-HTML response still yields what is in the prefix."""
        from app.services.readable_service import _extract_title
        assert _extract_title('<html><title>Only title</title>') == "Only title"

    def test_head_beyond_the_cap_is_given_up_on(self):
        from app.services.readable_service import _extract_title, _HEAD_SCAN_BYTES
        page = ("<html><head><script>%s</script>"
                '<meta property="og:title" content="Too far"></head></html>'
                % ("a" * (_HEAD_SCAN_BYTES + 1000)))
        assert _extract_title(page) is None


class TestAdoptResolvedUrl:
    def test_rewrites_a_saved_article(self):
        from app.services.saved_article_service import _adopt_resolved_url
        art = SimpleNamespace(feed_id=None, url="https://1gr.cz/log/score.aspx?id=x",
                              url_normalized="https://1gr.cz/log/score.aspx?id=x")
        _adopt_resolved_url(art, "https://www.idnes.cz/zpravy/story")
        assert art.url == "https://www.idnes.cz/zpravy/story"
        assert art.url_normalized == "https://www.idnes.cz/zpravy/story"

    def test_leaves_a_feed_article_alone(self):
        """A feed article's URL belongs to the feed; rewriting it would move the
        ground under the fetcher's own dedup."""
        from app.services.saved_article_service import _adopt_resolved_url
        art = SimpleNamespace(feed_id=7, url="https://feed.example/a",
                              url_normalized="https://feed.example/a")
        _adopt_resolved_url(art, "https://elsewhere.example/b")
        assert art.url == "https://feed.example/a"

    def test_no_resolved_url_is_a_no_op(self):
        from app.services.saved_article_service import _adopt_resolved_url
        art = SimpleNamespace(feed_id=None, url="https://ex.invalid/a",
                              url_normalized="https://ex.invalid/a")
        _adopt_resolved_url(art, None)
        assert art.url == "https://ex.invalid/a"

    def test_campaign_params_are_normalised_away(self):
        from app.services.saved_article_service import _adopt_resolved_url
        art = SimpleNamespace(feed_id=None, url="https://ex.invalid/x", url_normalized="https://ex.invalid/x")
        _adopt_resolved_url(art, "https://ex.invalid/a?utm_source=rss&id=7")
        assert art.url_normalized == "https://ex.invalid/a?id=7"


class TestDescriptionCapture:
    """og:description is the fallback the reader sees when extraction produced
    nothing to read, and the only thing a saved article can show as a list snippet
    (_make_snippet reads summary and content, never readable_content)."""

    DESC = "Institute declared a smog situation because of ground level ozone"

    def _article(self, **kw):
        from tests.test_saved_articles import make_article
        return make_article(**kw)

    def test_stored_on_a_feedless_article(self):
        from app.services.readable_service import apply_readable_result
        art = self._article(feed_id=None)
        apply_readable_result(art, "<p>Body</p>", None, None, description=self.DESC)
        assert art.summary == self.DESC

    def test_not_stored_on_a_feed_article(self):
        """Nothing writes summary for feed articles today; starting to would change
        snippets and search results across every feed."""
        from app.services.readable_service import apply_readable_result
        art = self._article(feed_id=7)
        art.summary = None
        apply_readable_result(art, "<p>Body</p>", None, None, description=self.DESC)
        assert art.summary is None

    def test_stored_even_when_extraction_failed(self):
        """This is the case it exists for — a consent page yields no body but the
        head still describes the article."""
        from app.services.readable_service import apply_readable_result, _WRONG_CONTENT_MSG
        art = self._article(feed_id=None)
        apply_readable_result(art, None, _WRONG_CONTENT_MSG, None, description=self.DESC)
        assert art.summary == self.DESC
        assert art.readable_content is None

    def test_returned_from_extraction(self):
        from app.services.readable_service import extract_readable_with_title
        page = ('<html><head><meta property="og:description" content="' + self.DESC
                + '"></head><body><p>Institute declared a smog situation because of '
                  'ground level ozone across the region today.</p></body></html>')
        with patch("app.services.readable_service._fetch_html",
                   return_value=(page, None, None, "https://x.invalid/a")):
            r = extract_readable_with_title("https://x.invalid/a")
        assert r.description == self.DESC

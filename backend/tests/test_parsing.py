"""Unit tests for app.utils.parsing helpers."""
import re

import pytest

from app.utils.parsing import NBSP_RUN_LIMIT, rewrite_relative_urls, soften_nbsp_runs


BASE = "https://example.com/2024/article-slug"


class TestRewriteRelativeUrls:
    def test_root_relative_img(self):
        html = '<img src="/images/photo.jpg">'
        assert rewrite_relative_urls(html, BASE) == '<img src="https://example.com/images/photo.jpg">'

    def test_root_relative_anchor(self):
        html = '<a href="/about">link</a>'
        assert rewrite_relative_urls(html, BASE) == '<a href="https://example.com/about">link</a>'

    def test_relative_path_img(self):
        html = '<img src="img/photo.jpg">'
        assert rewrite_relative_urls(html, BASE) == '<img src="https://example.com/2024/img/photo.jpg">'

    def test_absolute_url_unchanged(self):
        html = '<img src="https://cdn.other.com/img.png">'
        assert rewrite_relative_urls(html, BASE) == html

    def test_absolute_http_anchor_unchanged(self):
        html = '<a href="http://external.org/page">x</a>'
        assert rewrite_relative_urls(html, BASE) == html

    def test_mixed_content(self):
        html = (
            '<p><img src="/images/a.jpg"> '
            '<img src="https://cdn.example.com/b.jpg"> '
            '<a href="/page">p</a></p>'
        )
        result = rewrite_relative_urls(html, BASE)
        assert 'src="https://example.com/images/a.jpg"' in result
        assert 'src="https://cdn.example.com/b.jpg"' in result
        assert 'href="https://example.com/page"' in result

    def test_empty_string(self):
        assert rewrite_relative_urls("", BASE) == ""

    def test_no_urls(self):
        html = "<p>Just text.</p>"
        assert rewrite_relative_urls(html, BASE) == html

    def test_fragment_only_anchor(self):
        html = '<a href="#section">jump</a>'
        result = rewrite_relative_urls(html, BASE)
        assert result == '<a href="https://example.com/2024/article-slug#section">jump</a>'

    def test_multiple_attrs_on_one_tag(self):
        html = '<a href="/page" title="info">text</a>'
        result = rewrite_relative_urls(html, BASE)
        assert 'href="https://example.com/page"' in result

    def test_base_url_with_trailing_slash(self):
        html = '<img src="photo.jpg">'
        result = rewrite_relative_urls(html, "https://example.com/section/")
        assert result == '<img src="https://example.com/section/photo.jpg">'


NBSP = " "


def _runs(html: str) -> list[str]:
    """Text runs that a browser cannot break, longest first."""
    text = re.sub(r"<[^>]*>", " ", html).replace("&nbsp;", NBSP)
    parts = [p for p in re.split(r"[ \t\n]+", text) if p]
    return sorted(parts, key=len, reverse=True)


class TestSoftenNbspRuns:
    def test_long_run_is_split(self):
        html = f"<p>On Wednesday,{NBSP}Anthropic{NBSP}and{NBSP}AMD{NBSP}announced a deal.</p>"
        result = soften_nbsp_runs(html)
        assert len(_runs(result)[0]) <= NBSP_RUN_LIMIT

    def test_run_spanning_inline_tags_is_split(self):
        html = (f"<p>On Wednesday,{NBSP}<strong>Anthropic</strong>{NBSP}and{NBSP}"
                f"<strong>AMD</strong>{NBSP}announced a deal.</p>")
        result = soften_nbsp_runs(html)
        assert result.count(NBSP) < html.count(NBSP)

    def test_entity_form_is_handled(self):
        html = "<p>Hello&nbsp;world&nbsp;this&nbsp;is&nbsp;a&nbsp;long&nbsp;unbreakable&nbsp;phrase.</p>"
        result = soften_nbsp_runs(html)
        assert len(_runs(result)[0]) <= NBSP_RUN_LIMIT

    def test_very_long_run_splits_recursively(self):
        words = NBSP.join(f"word{i}" for i in range(20))
        result = soften_nbsp_runs(f"<p>{words}</p>")
        assert len(_runs(result)[0]) <= NBSP_RUN_LIMIT

    def test_short_run_untouched(self):
        html = f"<p>Trasa je 10{NBSP}km dlouha.</p>"
        assert soften_nbsp_runs(html) == html

    def test_units_and_prepositions_kept(self):
        html = (f"<p>Namerili{NBSP}jsme{NBSP}presne{NBSP}10{NBSP}km{NBSP}a{NBSP}"
                f"pak{NBSP}dalsich{NBSP}50{NBSP}%{NBSP}navic.</p>")
        result = soften_nbsp_runs(html)
        assert f"10{NBSP}km" in result
        assert f"50{NBSP}%" in result
        assert f"a{NBSP}pak" in result
        assert len(_runs(result)[0]) <= NBSP_RUN_LIMIT

    def test_run_stops_at_block_boundary(self):
        html = f"<p>alpha beta{NBSP}gamma</p><p>delta{NBSP}epsilon zeta</p>"
        assert soften_nbsp_runs(html) == html

    def test_run_stops_at_br(self):
        html = f"<p>alpha beta{NBSP}gamma<br>delta{NBSP}epsilon zeta</p>"
        assert soften_nbsp_runs(html) == html

    def test_attributes_untouched(self):
        html = f'<p><a href="https://example.com/a{NBSP}very{NBSP}long{NBSP}path{NBSP}here">x</a></p>'
        assert soften_nbsp_runs(html) == html

    def test_no_nbsp_returns_input(self):
        html = "<p>Plain sentence with ordinary spaces.</p>"
        assert soften_nbsp_runs(html) is html

    def test_empty_input(self):
        assert soften_nbsp_runs("") == ""

    def test_idempotent(self):
        html = f"<p>On Wednesday,{NBSP}Anthropic{NBSP}and{NBSP}AMD{NBSP}announced a deal.</p>"
        once = soften_nbsp_runs(html)
        assert soften_nbsp_runs(once) == once

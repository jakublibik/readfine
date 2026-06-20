"""Unit tests for app.utils.parsing helpers."""
import pytest

from app.utils.parsing import rewrite_relative_urls


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

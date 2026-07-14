"""Shared Markdown → HTML renderer (mistune)."""
import mistune as _mistune_module

_renderer = _mistune_module.create_markdown(escape=True)


def md_render(text: str) -> str:
    return _renderer(text)


def md_render_inline(text: str) -> str:
    """Render a single line of Markdown without the wrapping block <p>.

    For short prose (feature descriptions, labels) where a paragraph tag would
    add a block box. Falls back to the full render if the result isn't a single
    paragraph.
    """
    html = md_render(text).strip()
    if html.startswith("<p>") and html.endswith("</p>") and html.count("<p>") == 1:
        return html[3:-4]
    return html

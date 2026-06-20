"""Shared Markdown → HTML renderer (mistune)."""
import mistune as _mistune_module

_renderer = _mistune_module.create_markdown(escape=True)


def md_render(text: str) -> str:
    return _renderer(text)

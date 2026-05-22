import mistune
from markupsafe import Markup
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

_md_render = mistune.create_markdown(escape=True)

templates.env.filters["markdown"] = lambda text: Markup(_md_render(text or ""))

_ai_enabled: bool = False


def get_ai_enabled() -> bool:
    return _ai_enabled


def set_ai_enabled(value: bool) -> None:
    global _ai_enabled
    _ai_enabled = value


templates.env.globals["app_ai_enabled"] = get_ai_enabled

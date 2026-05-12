from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

_ai_enabled: bool = False


def get_ai_enabled() -> bool:
    return _ai_enabled


def set_ai_enabled(value: bool) -> None:
    global _ai_enabled
    _ai_enabled = value


templates.env.globals["app_ai_enabled"] = get_ai_enabled

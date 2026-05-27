import json
import mistune
from markupsafe import Markup
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

_md_render = mistune.create_markdown(escape=True)

templates.env.filters["markdown"] = lambda text: Markup(_md_render(text or ""))


def _catchup_config_json(cfg) -> str:
    """Serialize UserCatchupConfig to a JSON string safe for use in data-* attributes."""
    data = {
        "id": cfg.id,
        "name": cfg.name,
        "scope_include": cfg.scope_include or "",
        "period": cfg.period,
        "filter_status": cfg.filter_status,
        "filter_labeled": cfg.filter_labeled,
        "filter_score_min": cfg.filter_score_min,
        "article_limit": cfg.article_limit,
        "model_slot": cfg.model_slot,
        "custom_prompt": cfg.custom_prompt or "",
        "include_snippet": cfg.include_snippet,
    }
    return json.dumps(data, ensure_ascii=False)


templates.env.filters["catchup_config_json"] = _catchup_config_json

_ai_enabled: bool = False


def get_ai_enabled() -> bool:
    return _ai_enabled


def set_ai_enabled(value: bool) -> None:
    global _ai_enabled
    _ai_enabled = value


templates.env.globals["app_ai_enabled"] = get_ai_enabled

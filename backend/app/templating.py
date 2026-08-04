import json
from markupsafe import Markup
from jinja2 import Undefined
from fastapi.templating import Jinja2Templates

from app.utils.markdown import md_render, md_render_inline
from app.utils.request_context import current_viewer_is_admin, current_viewer_ai_error
from app.utils.static import static_url
from app.utils.datetime_format import (
    format_local,
    current_viewer_tz,
    timezone_groups,
    is_common_timezone,
)
from app.utils.formats import format_number, format_number_g, format_choices
from app.utils.form_guard import HONEYPOT_FIELD, issue_form_ts
from app.fetcher.failure import BLOCK_BADGE_THRESHOLD, BLOCK_DISABLE_THRESHOLD

templates = Jinja2Templates(directory="app/templates")

templates.env.filters["markdown"] = lambda text: Markup(md_render(text or ""))
templates.env.filters["markdown_inline"] = lambda text: Markup(md_render_inline(text or ""))


def _localtime(dt, fmt: str = "short") -> str:
    """Format a datetime in the current request's viewer timezone."""
    return format_local(dt, current_viewer_tz.get(), fmt)


def _utctime(dt, fmt: str = "short") -> str:
    """Format a datetime in UTC (operational/admin logs)."""
    return format_local(dt, "UTC", fmt)


templates.env.filters["localtime"] = _localtime
templates.env.filters["utctime"] = _utctime
# A missing template value arrives as jinja Undefined, whose __float__ raises
# UndefinedError (not caught by format_number); coerce it to None so a stray
# {{ missing|num }} renders empty like a bare {{ missing }} instead of 500ing.
templates.env.filters["num"] = lambda value, decimals=None: format_number(
    None if isinstance(value, Undefined) else value, decimals
)
templates.env.filters["numg"] = lambda value: format_number_g(
    None if isinstance(value, Undefined) else value
)


def _catchup_config_json(cfg) -> str:
    """Serialize UserCatchupConfig to a JSON string safe for use in data-* attributes."""
    data = {
        "id": cfg.id,
        "name": cfg.name,
        "scope_include": cfg.scope_include or "",
        "period": cfg.period,
        "filter_status": cfg.filter_status,
        "label_filter": cfg.label_filter or "",
        "filter_score_min": cfg.filter_score_min,
        "article_limit": cfg.article_limit,
        "model_slot": cfg.model_slot,
        "custom_prompt": cfg.custom_prompt or "",
        "include_snippet": cfg.include_snippet,
    }
    return json.dumps(data, ensure_ascii=False)


templates.env.filters["catchup_config_json"] = _catchup_config_json


def _parse_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


templates.env.filters["parse_json_list"] = _parse_json_list


def _hostname(url: str | None) -> str:
    """Bare host for a URL, used as the source label on articles with no feed.

    A saved-by-URL article has no feed title, and "Unknown feed" would be actively
    wrong — it never had one. The host is what the reader actually wants to see.
    """
    if not url:
        return ""
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).netloc or "").removeprefix("www.")
    except ValueError:
        return ""


templates.env.filters["hostname"] = _hostname

_ai_enabled: bool = False


def get_ai_enabled() -> bool:
    return _ai_enabled


def set_ai_enabled(value: bool) -> None:
    global _ai_enabled
    _ai_enabled = value


# Whether the in-app feedback link should show: admin enabled it AND SMTP is
# configured (otherwise the message couldn't be delivered). Mirrors the
# AppSettings singleton; refreshed at startup and on every admin settings save.
_feedback_available: bool = False


def get_feedback_available() -> bool:
    return _feedback_available


def set_feedback_available(value: bool) -> None:
    global _feedback_available
    _feedback_available = value


templates.env.globals["static_url"] = static_url
templates.env.globals["app_ai_enabled"] = get_ai_enabled
templates.env.globals["app_feedback_available"] = get_feedback_available
templates.env.globals["viewer_is_admin"] = lambda: current_viewer_is_admin.get()
templates.env.globals["ai_error_fresh"] = lambda: current_viewer_ai_error.get()
templates.env.globals["timezone_groups"] = timezone_groups
templates.env.globals["is_common_timezone"] = is_common_timezone
templates.env.globals["format_choices"] = format_choices
# Bot traps for public forms. Called from the template so every render (including
# a re-render after a validation error) gets a fresh stamp.
templates.env.globals["form_ts"] = issue_form_ts
templates.env.globals["honeypot_field"] = HONEYPOT_FIELD
# How many consecutive host refusals before a feed's badge says so, and how many
# before it is switched off. Exposed rather than hard-coded in the feed tables so
# the thresholds live in one place.
templates.env.globals["block_badge_threshold"] = BLOCK_BADGE_THRESHOLD
templates.env.globals["block_disable_threshold"] = BLOCK_DISABLE_THRESHOLD

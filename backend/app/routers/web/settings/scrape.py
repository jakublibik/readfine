"""Web routes for the scrape-feed setup flow (preview, AI selector, subscribe)."""

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_ai_enabled
from app.database import get_db
from app.fetcher.scrape import extract_article_links, fetch_page_html
from app.models.settings import AppSettings
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.services.ai_service import generate_css_selector_from_sample, get_ai_client
from app.services.feed import subscribe_scrape
from app.templating import templates
from app.utils.parsing import safe_int
from app.utils.scrape_ai import build_selector_prompt, extract_article_sample, generate_selector_prompt
from app.utils.url_validator import split_url_credentials

from .common import (
    _ai_selector_available,
    _ensure_scheme,
    _get_feeds_context,
    _scrape_target,
    _snap_interval,
)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/feeds/scrape-setup", response_class=HTMLResponse)
@limiter.limit("20/minute")
async def settings_scrape_setup(
    request: Request,
    url: str = "",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not url.startswith(("http://", "https://")):
        return RedirectResponse("/settings/feeds", status_code=303)

    # Credentials in the address are used to fetch the page and go back into the form
    # the user is filling in, since that is the only place a scrape feed can be told
    # about them. Everything else on this page gets the clean address: the AI prompt
    # (which leaves for a provider) and the page title (which becomes the feed's name).
    clean_url, auth_user, auth_pass = split_url_credentials(url)
    auth = (auth_user, auth_pass) if auth_user is not None else None

    _, folders, _ = await _get_feeds_context(user, db)
    html = ""
    page_title = ""
    prompt = ""
    fetch_error = None

    html_sample = ""
    try:
        html = await fetch_page_html(clean_url, auth=auth)
        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("title")
        page_title = title_tag.get_text(strip=True)[:255] if title_tag else clean_url
        prompt = generate_selector_prompt(clean_url, html)
        html_sample = extract_article_sample(html)
    except Exception as e:
        fetch_error = str(e)

    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    user_s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    ai_selector_available = _ai_selector_available(app_s, user_s)

    return templates.TemplateResponse(request, "settings/scrape_setup.html", {
        "url": url,
        "page_title": page_title,
        "prompt": prompt,
        "html_sample": html_sample,
        "fetch_error": fetch_error,
        "folders": folders,
        "ai_selector_available": ai_selector_available,
        "default_fetch_interval_min": (app_s.default_fetch_interval_min if app_s else None) or 60,
    })


@router.post("/feeds/scrape-preview", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def settings_scrape_preview(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    url, auth = await _scrape_target(form, user, db)
    selector = (form.get("selector") or form.get("article_links_selector") or "").strip()

    if not url or not selector:
        return templates.TemplateResponse(request, "settings/partials/scrape_preview.html", {
            "error": "URL and selector are required.",
        })

    try:
        html = await fetch_page_html(url, auth=auth)
        links = extract_article_links(html, selector, url)
    except Exception as e:
        return templates.TemplateResponse(request, "settings/partials/scrape_preview.html", {
            "error": f"Failed to fetch page: {e}",
        })

    return templates.TemplateResponse(request, "settings/partials/scrape_preview.html", {
        "links": [(u, t) for u, t, *_ in links[:10]],
        "total": len(links),
        "selector": selector,
    })


@router.post("/feeds/scrape-ai-selector", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def settings_scrape_ai_selector(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai: None = Depends(require_ai_enabled),
):
    import json as _json
    from app.models.article import AiUsageLog

    form = await request.form()
    url, auth = await _scrape_target(form, user, db)
    html_sample = (form.get("html_sample") or "").strip()
    history_raw = (form.get("conversation_history") or "[]").strip()

    if not url:
        return HTMLResponse("<div class='px-4 py-3 bg-red-50 border border-red-200 rounded text-sm text-red-700'>URL is required.</div>")

    try:
        history: list[dict] = _json.loads(history_raw)
        if not isinstance(history, list):
            history = []
    except Exception:
        history = []

    if not html_sample:
        try:
            html = await fetch_page_html(url, auth=auth)
            html_sample = extract_article_sample(html)
        except Exception as e:
            prompt_text = ""
            return templates.TemplateResponse(request, "settings/partials/scrape_ai_error.html", {
                "error": f"Could not fetch page: {e}",
                "prompt_text": prompt_text,
            })

    client, provider, model = await get_ai_client(user.id, "quality", db)
    if client is None:
        return HTMLResponse("<div class='px-4 py-3 bg-red-50 border border-red-200 rounded text-sm text-red-700'>Quality model not configured. Set it in <a href='/settings/ai' class='underline'>Settings → AI</a>.</div>")

    in_tok = out_tok = 0
    try:
        selector, in_tok, out_tok = await generate_css_selector_from_sample(
            url, html_sample, history, client, provider, model
        )
    except Exception as e:
        db.add(AiUsageLog(
            user_id=user.id, operation="css_selector_generation",
            model_slot="quality", model=model, provider=provider,
            input_tokens=in_tok, output_tokens=out_tok,
        ))
        await db.commit()
        prompt_text = build_selector_prompt(url, html_sample, history)
        return templates.TemplateResponse(request, "settings/partials/scrape_ai_error.html", {
            "error": f"AI error: {e}",
            "prompt_text": prompt_text,
        })

    # Validate: empty, too long, or looks like prose
    # Prose heuristic: starts with capital letter followed by space, or
    # contains spaces but none of the chars that are CSS-only
    css_only = set('>.#[+~')
    looks_like_prose = (
        (len(selector) > 1 and selector[0].isupper() and selector[1] == ' ')
        or (' ' in selector and not any(c in selector for c in css_only))
    )
    is_valid = bool(selector) and len(selector) <= 300 and not looks_like_prose

    db.add(AiUsageLog(
        user_id=user.id, operation="css_selector_generation",
        model_slot="quality", model=model, provider=provider,
        input_tokens=in_tok, output_tokens=out_tok,
    ))
    await db.commit()

    if not is_valid:
        prompt_text = build_selector_prompt(url, html_sample, history)
        ai_explanation = selector if selector else None
        return templates.TemplateResponse(request, "settings/partials/scrape_ai_error.html", {
            "error": "Could not generate a valid selector.",
            "ai_explanation": ai_explanation,
            "prompt_text": prompt_text,
        })

    updated_history = history + [{"selector": selector, "feedback": ""}]
    updated_history_json = _json.dumps(updated_history)
    prompt_text = build_selector_prompt(url, html_sample, history)

    response = templates.TemplateResponse(request, "settings/partials/scrape_ai_result.html", {
        "selector": selector,
        "updated_history_json": updated_history_json,
        "html_sample": html_sample,
        "prompt_text": prompt_text,
    })
    response.headers["HX-Trigger"] = _json.dumps({"selectorGenerated": {"selector": selector}})
    return response


@router.post("/feeds/scrape-show-prompt", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def settings_scrape_show_prompt(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    url, auth = await _scrape_target(form, user, db)

    if not url:
        return HTMLResponse("<div class='px-4 py-3 bg-red-50 border border-red-200 rounded text-sm text-red-700'>URL is required.</div>")

    try:
        html = await fetch_page_html(url, auth=auth)
        prompt = generate_selector_prompt(url, html)
    except Exception as e:
        return HTMLResponse(f"<div class='px-4 py-3 bg-red-50 border border-red-200 rounded text-sm text-red-700'>Could not fetch page: {e}</div>")

    return templates.TemplateResponse(request, "settings/partials/scrape_prompt.html", {
        "prompt": prompt,
    })


@router.post("/feeds/scrape", response_class=HTMLResponse)
async def settings_scrape_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    url = form.get("url", "").strip()
    url = _ensure_scheme(url)
    # subscribe_scrape splits the credentials out for storage; the clean address is
    # needed here too, so a feed the user gave no name is not named after its password.
    clean_url, auth_user, auth_pass = split_url_credentials(url)
    auth = (auth_user, auth_pass) if auth_user is not None else None
    selector = form.get("selector", "").strip()
    title = form.get("title", "").strip() or clean_url
    folder_id = safe_int(form.get("folder_id"))
    interval_raw = safe_int(form.get("fetch_interval_min"))
    fetch_interval_min = _snap_interval(interval_raw) if interval_raw else None

    _, folders, _ = await _get_feeds_context(user, db)
    try:
        await subscribe_scrape(user=user, url=url, selector=selector, title=title,
                               folder_id=folder_id, fetch_interval_min=fetch_interval_min, db=db)
        from urllib.parse import quote
        return RedirectResponse(f"/settings/feeds?added={quote(title)}", status_code=303)
    except ValueError as e:
        html_sample = ""
        try:
            html = await fetch_page_html(clean_url, auth=auth)
            soup = BeautifulSoup(html, "lxml")
            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True)[:255] if title_tag else clean_url
            prompt = generate_selector_prompt(clean_url, html)
            html_sample = extract_article_sample(html)
        except Exception:
            page_title = title
            prompt = ""
        app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
        user_s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
        ai_selector_available = _ai_selector_available(app_s, user_s)
        return templates.TemplateResponse(request, "settings/scrape_setup.html", {
            "url": url,
            "page_title": page_title,
            "prompt": prompt,
            "html_sample": html_sample,
            "selector": selector,
            "title": title,
            "folder_id": folder_id,
            "fetch_interval_min": fetch_interval_min,
            "folders": folders,
            "error": str(e),
            "ai_selector_available": ai_selector_available,
            "default_fetch_interval_min": (app_s.default_fetch_interval_min if app_s else None) or 60,
        })

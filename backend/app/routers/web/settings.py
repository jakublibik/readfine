"""Web routes for settings: feeds, folders, labels, filters, API tokens, and OPML."""
import logging
import secrets
from datetime import datetime, timezone

import asyncio
import httpx

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import feedparser
from bs4 import BeautifulSoup
from sqlalchemy import select

from app.auth.security import hash_password, verify_password, hash_token
from app.config import settings as app_settings_config
from app.rate_limit import limiter
from app.utils.crypto import encrypt
from app.utils.parsing import safe_int
from app.utils.url_validator import async_validate_feed_url, fetch_url_with_ssrf_check
from app.utils.feed_detect import detect_feeds
from app.utils.scrape_ai import generate_selector_prompt
from app.fetcher.scrape import extract_article_links

logger = logging.getLogger(__name__)

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.auth import ApiToken
from app.models.feed import Folder, UserFeed
from app.models.label import Label
from app.models.user import User, UserSettings
from app.schemas.filter import FilterActionCreate, FilterConditionCreate, FilterCreate, FilterUpdate
from app.schemas.label import LabelCreate, LabelUpdate
from app.services.feed import list_user_feeds, subscribe, subscribe_scrape, unsubscribe
from app.services.filter_service import (
    apply_filter_retroactively,
    create_filter,
    delete_filter,
    get_filter,
    list_filters,
    test_filter,
    update_filter,
)
from app.services.label_service import (
    create_label,
    delete_label,
    list_labels,
    update_label,
)
from app.services.opml import MAX_UPLOAD_BYTES, ImportResult, export_opml, import_opml
from app.services.ai_service import (
    PROVIDER_DOCS_URLS,
    SUPPORTED_PROVIDERS,
    delete_api_key,
    estimate_monthly_cost,
    generate_preference_text,
    get_ai_client,
    list_api_keys,
    save_api_key,
    verify_ai_slot,
)

router = APIRouter(prefix="/settings", tags=["settings"])
templates = Jinja2Templates(directory="app/templates")


# ── Labels ────────────────────────────────────────────────────────────────────

@router.get("/labels", response_class=HTMLResponse)
async def settings_labels(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/labels.html", {"labels": labels})


@router.post("/labels", response_class=HTMLResponse)
async def settings_labels_create(
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = name.strip()
    existing = await db.scalar(select(Label).where(Label.user_id == user.id, Label.name == name))
    if existing:
        labels = await list_labels(user, db)
        return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
            "labels": labels,
            "error": f'A label named "{name}" already exists.',
        })
    await create_label(user, LabelCreate(name=name, color=color), db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
        "labels": labels,
        "success": f'Label "{name}" added.',
    })


@router.post("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_update(
    label_id: int,
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = name.strip()
    conflict = await db.scalar(
        select(Label).where(Label.user_id == user.id, Label.name == name, Label.id != label_id)
    )
    if conflict:
        labels = await list_labels(user, db)
        return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
            "labels": labels,
            "error": f'A label named "{name}" already exists.',
        })
    await update_label(user, label_id, LabelUpdate(name=name, color=color), db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})


@router.delete("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_delete(
    label_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_label(user, label_id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})


# ── Feeds ─────────────────────────────────────────────────────────────────────

async def _get_feeds_context(user, db):
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return user_feeds, folders


@router.get("/feeds", response_class=HTMLResponse)
async def settings_feeds(
    request: Request,
    added: str = "",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/feeds.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "error": None,
        "subscribe_url": "",
        "detected_feeds": [],
        "added": added,
    })


@router.post("/feeds/test", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def settings_feeds_test(
    request: Request,
    url: str = Form(""),
    fetch_auth_user: str = Form(""),
    fetch_auth_pass: str = Form(""),
    user: User = Depends(get_current_user),
):
    """Test a feed URL without saving. Returns title + entry count or error."""
    url = url.strip()
    if not url:
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": "Please enter a URL."})
    auth_user = fetch_auth_user.strip() or None
    auth_pass = fetch_auth_pass or None

    try:
        await async_validate_feed_url(url)
    except ValueError as e:
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": str(e)})

    _headers = {
        "User-Agent": "Readfine/1.0",
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    has_auth = bool(auth_user and auth_pass)
    auth = (auth_user, auth_pass) if has_auth else None
    loop = asyncio.get_running_loop()

    async def _fetch(with_auth) -> tuple[str | None, str | None]:
        """Returns (content, error_string). Uses SSRF-safe redirect loop."""
        fetch_auth = auth if with_auth else None
        try:
            content = await loop.run_in_executor(
                None,
                lambda: fetch_url_with_ssrf_check(url, auth=fetch_auth, timeout=15, headers=_headers),
            )
            return content, None
        except httpx.HTTPStatusError as e:
            sc = e.response.status_code
            if sc == 403:
                return None, "HTTP 403: Access denied — the server is likely blocking requests from this host (geo-block or datacenter IP block)."
            return None, f"HTTP {sc}: {e.response.reason_phrase}"
        except (httpx.RequestError, ValueError) as e:
            return None, f"Connection error: {e}"

    # Always fetch with the configured auth (or no auth if none provided)
    content, error = await _fetch(with_auth=True)

    auth_status = None  # will be set when credentials were provided
    if has_auth and content is None and error and "401" in error:
        # Credentials provided but got 401 → wrong credentials
        auth_status = "wrong"
    elif has_auth and content is not None:
        # Succeeded with auth — check if auth was actually needed
        no_auth_content, no_auth_error = await _fetch(with_auth=False)
        if no_auth_content is not None:
            auth_status = "not_required"
        else:
            auth_status = "required_ok"

    if error and auth_status != "wrong":
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": error})

    if auth_status == "wrong":
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": f"{error} — credentials rejected"})

    parsed = await loop.run_in_executor(None, feedparser.parse, content)

    import xml.sax._exceptions as _sax
    is_xml_error = parsed.bozo and isinstance(parsed.bozo_exception, _sax.SAXParseException)
    is_empty_feed = parsed.bozo and not parsed.entries and not parsed.feed

    if is_xml_error or is_empty_feed:
        # Not RSS — try to detect RSS feeds linked from the page
        detected_feeds = []
        try:
            detected_feeds = await detect_feeds(url)
        except Exception:
            pass
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html", {
            "detected_feeds": detected_feeds,
            "scrape_available": url.startswith(("http://", "https://")),
            "test_url": url,
        })

    feed_title = parsed.feed.get("title") or url
    entry_count = len(parsed.entries)
    return templates.TemplateResponse(request, "settings/partials/feed_test_result.html", {
        "feed_title": feed_title,
        "entry_count": entry_count,
        "auth_status": auth_status,
    })


@router.post("/feeds", response_class=HTMLResponse)
async def settings_feeds_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    url = form.get("url", "").strip()
    custom_title = form.get("custom_title", "").strip() or None
    folder_id_raw = form.get("folder_id")
    folder_id = safe_int(folder_id_raw)
    fetch_auth_user = form.get("fetch_auth_user", "").strip() or None
    fetch_auth_pass = form.get("fetch_auth_pass", "") or None
    is_private = form.get("is_private") == "on"

    user_feeds, folders = await _get_feeds_context(user, db)
    error = None
    detected_feeds = []
    try:
        uf = await subscribe(user=user, url=url, folder_id=folder_id,
                             custom_title=custom_title, fetch_auth_user=fetch_auth_user,
                             fetch_auth_pass=fetch_auth_pass, is_private=is_private, db=db)
        from urllib.parse import quote
        return RedirectResponse(f"/settings/feeds?added={quote(uf.feed.title)}", status_code=303)
    except ValueError as e:
        error = str(e)
        # Try to detect RSS feeds linked from the page (only for parse/fetch failures)
        if "valid RSS" in error or "valid feed" in error or "Not a valid" in error or "parse" in error.lower():
            try:
                detected_feeds = await detect_feeds(url)
            except Exception:
                pass
    except httpx.HTTPStatusError as e:
        status = e.response.status_code
        if status == 404:
            error = "Feed not found (404). The URL may no longer exist."
        elif status == 403:
            error = "Access denied (403) — the server is likely blocking requests from this host (geo-block or datacenter IP block)."
        elif status == 401:
            error = "Authentication required (401). Try adding HTTP credentials."
        else:
            error = f"HTTP error {status} when fetching the feed."
        try:
            detected_feeds = await detect_feeds(url)
        except Exception:
            pass
    except Exception as e:
        logger.error("Unexpected error during feed subscribe (url=%s): %s", url, e)
        error = "Could not subscribe to feed. Please check the URL and try again."

    # Re-fetch after failed subscribe (no commit happened)
    user_feeds, folders = await _get_feeds_context(user, db)
    is_rss_error = error and any(k in error for k in ("valid RSS", "valid feed", "Not a valid", "parse", "404", "403", "HTTP error"))
    show_scrape_option = (
        is_rss_error
        and not detected_feeds
        and url.startswith(("http://", "https://"))
    )
    return templates.TemplateResponse(request, "settings/feeds.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "error": error if not detected_feeds else None,
        "subscribe_url": url,
        "detected_feeds": detected_feeds,
        "show_scrape_option": show_scrape_option,
    })


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

    _, folders = await _get_feeds_context(user, db)
    loop = asyncio.get_running_loop()
    html = ""
    page_title = ""
    prompt = ""
    fetch_error = None

    try:
        html = await loop.run_in_executor(
            None, fetch_url_with_ssrf_check, url, None, 30,
            {"User-Agent": "Readfine/1.0", "Accept": "text/html,*/*"},
        )
        soup = BeautifulSoup(html, "lxml")
        title_tag = soup.find("title")
        page_title = title_tag.get_text(strip=True)[:255] if title_tag else url
        prompt = generate_selector_prompt(url, html)
    except Exception as e:
        fetch_error = str(e)

    return templates.TemplateResponse(request, "settings/scrape_setup.html", {
        "url": url,
        "page_title": page_title,
        "prompt": prompt,
        "fetch_error": fetch_error,
        "folders": folders,
    })


@router.post("/feeds/scrape-preview", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def settings_scrape_preview(
    request: Request,
    user: User = Depends(get_current_user),
):
    form = await request.form()
    url = form.get("url", "").strip()
    selector = form.get("selector", "").strip()

    if not url or not selector:
        return templates.TemplateResponse(request, "settings/partials/scrape_preview.html", {
            "error": "URL and selector are required.",
        })

    loop = asyncio.get_running_loop()
    try:
        html = await loop.run_in_executor(
            None, fetch_url_with_ssrf_check, url, None, 30,
            {"User-Agent": "Readfine/1.0", "Accept": "text/html,*/*"},
        )
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


@router.post("/feeds/scrape", response_class=HTMLResponse)
async def settings_scrape_subscribe(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    url = form.get("url", "").strip()
    selector = form.get("selector", "").strip()
    title = form.get("title", "").strip() or url
    folder_id = safe_int(form.get("folder_id"))
    interval_raw = safe_int(form.get("fetch_interval_min"))
    fetch_interval_min = max(15, min(1440, round(interval_raw / 15) * 15)) if interval_raw else None

    _, folders = await _get_feeds_context(user, db)
    try:
        await subscribe_scrape(user=user, url=url, selector=selector, title=title,
                               folder_id=folder_id, fetch_interval_min=fetch_interval_min, db=db)
        from urllib.parse import quote
        return RedirectResponse(f"/settings/feeds?added={quote(title)}", status_code=303)
    except ValueError as e:
        loop = asyncio.get_running_loop()
        try:
            html = await loop.run_in_executor(
                None, fetch_url_with_ssrf_check, url, None, 30,
                {"User-Agent": "Readfine/1.0", "Accept": "text/html,*/*"},
            )
            soup = BeautifulSoup(html, "lxml")
            title_tag = soup.find("title")
            page_title = title_tag.get_text(strip=True)[:255] if title_tag else url
            prompt = generate_selector_prompt(url, html)
        except Exception:
            page_title = title
            prompt = ""
        return templates.TemplateResponse(request, "settings/scrape_setup.html", {
            "url": url,
            "page_title": page_title,
            "prompt": prompt,
            "selector": selector,
            "title": title,
            "folder_id": folder_id,
            "fetch_interval_min": fetch_interval_min,
            "folders": folders,
            "error": str(e),
        })


@router.get("/feeds/{user_feed_id}/edit", response_class=HTMLResponse)
async def settings_feed_edit(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserFeed)
        .where(UserFeed.id == user_feed_id, UserFeed.user_id == user.id)
        .options(selectinload(UserFeed.feed))
    )
    uf = result.scalar_one_or_none()
    if not uf:
        return HTMLResponse("<p class='text-red-500 p-4'>Feed not found.</p>", status_code=404)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return templates.TemplateResponse(request, "settings/feed_edit.html", {
        "uf": uf,
        "folders": folders,
        "is_sole_subscriber": uf.feed.subscriber_count == 1,
    })


@router.post("/feeds/{user_feed_id}/edit", response_class=HTMLResponse)
async def settings_feed_update(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserFeed)
        .where(UserFeed.id == user_feed_id, UserFeed.user_id == user.id)
        .options(selectinload(UserFeed.feed))
    )
    uf = result.scalar_one_or_none()
    if not uf:
        return HTMLResponse("<p class='text-red-500 p-4'>Feed not found.</p>", status_code=404)

    form = await request.form()
    custom_title = form.get("custom_title", "").strip() or None
    folder_id_raw = form.get("folder_id")
    folder_id = safe_int(folder_id_raw)

    if folder_id is not None:
        folder_check = await db.execute(
            select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
        )
        if not folder_check.scalar_one_or_none():
            folder_id = None

    uf.custom_title = custom_title
    uf.folder_id = folder_id
    uf.extract_readable = form.get("extract_readable") == "true"
    uf.readable_auto_disabled = False

    interval_raw = safe_int(form.get("fetch_interval_min"))
    if interval_raw is not None:
        uf.feed.fetch_interval_min = max(15, min(1440, round(interval_raw / 15) * 15))
    else:
        uf.feed.fetch_interval_min = None

    if uf.feed.is_private or uf.feed.subscriber_count == 1:
        fetch_auth_user = form.get("fetch_auth_user", "").strip() or None
        fetch_auth_pass = form.get("fetch_auth_pass", "") or None
        uf.feed.fetch_auth_user = fetch_auth_user
        if fetch_auth_pass:
            uf.feed.fetch_auth_pass_encrypted = encrypt(fetch_auth_pass)
        if (fetch_auth_user or fetch_auth_pass) and not uf.feed.is_private:
            uf.feed.is_private = True

    if uf.feed.feed_type == "scrape" and (uf.feed.is_private or uf.feed.subscriber_count == 1):
        new_selector = form.get("article_links_selector", "").strip()
        if new_selector:
            uf.feed.type_config = {**(uf.feed.type_config or {}), "article_links_selector": new_selector}

    if uf.feed.status == "disabled":
        uf.feed.status = "active"
    uf.feed.fetch_error_count = 0

    await db.commit()
    return RedirectResponse("/settings/feeds", status_code=303)


@router.delete("/feeds/{user_feed_id}", response_class=HTMLResponse)
async def settings_feed_delete(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await unsubscribe(user, user_feed_id, db)
    except ValueError:
        pass
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


# ── Folders ───────────────────────────────────────────────────────────────────

@router.post("/folders", response_class=HTMLResponse)
async def settings_folder_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        existing = await db.execute(
            select(Folder).where(Folder.user_id == user.id, Folder.name == name)
        )
        if not existing.scalar_one_or_none():
            db.add(Folder(user_id=user.id, name=name))
            await db.commit()
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


@router.delete("/folders/{folder_id}", response_class=HTMLResponse)
async def settings_folder_delete(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if folder:
        await db.delete(folder)
        await db.commit()
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


@router.get("/feeds-list", response_class=HTMLResponse)
async def settings_feeds_list(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


@router.get("/folders/{folder_id}/rename-form", response_class=HTMLResponse)
async def settings_folder_rename_form(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if not folder:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "settings/partials/folder_rename_form.html", {
        "folder": folder,
    })


@router.post("/folders/{folder_id}/rename", response_class=HTMLResponse)
async def settings_folder_rename(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    name = form.get("name", "").strip()
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if folder and name:
        folder.name = name
        await db.commit()
    user_feeds, folders = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
    })


# ── Filters ───────────────────────────────────────────────────────────────────

@router.get("/filters", response_class=HTMLResponse)
async def settings_filters(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    filters = await list_filters(user.id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/filters.html", {
        "filters": filters,
        "labels": labels,
    })


@router.get("/filters/new", response_class=HTMLResponse)
async def settings_filter_new(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    labels = await list_labels(user, db)
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return templates.TemplateResponse(request, "settings/filter_edit.html", {
        "filter": None,
        "labels": labels,
        "user_feeds": user_feeds,
        "folders": folders,
    })


@router.get("/filters/{filter_id}/edit", response_class=HTMLResponse)
async def settings_filter_edit(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    f = await get_filter(user.id, filter_id, db)
    if not f:
        return HTMLResponse("<p class='text-red-500 p-4'>Filter not found.</p>", status_code=404)
    labels = await list_labels(user, db)
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    folders = folders_result.scalars().all()
    return templates.TemplateResponse(request, "settings/filter_edit.html", {
        "filter": f,
        "labels": labels,
        "user_feeds": user_feeds,
        "folders": folders,
    })


async def _filter_form_context(user, db):
    labels = await list_labels(user, db)
    user_feeds = await list_user_feeds(user, db)
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(Folder.position, Folder.name)
    )
    return {"labels": labels, "user_feeds": user_feeds, "folders": folders_result.scalars().all()}


@router.post("/filters", response_class=HTMLResponse)
async def settings_filter_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    save_and_test = form.get("save_and_test") == "1"
    payload = _parse_filter_form(form)
    try:
        f = await create_filter(user.id, payload, db)
    except ValueError as e:
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": None, "error": str(e)})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx, status_code=422)
    if save_and_test:
        test_result = await test_filter(user.id, f.id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": f, "test_result": test_result})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)
    return RedirectResponse("/settings/filters", status_code=303)


@router.post("/filters/{filter_id}/edit", response_class=HTMLResponse)
async def settings_filter_update(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    save_and_test = form.get("save_and_test") == "1"
    payload = _parse_filter_form(form)
    try:
        await update_filter(user.id, filter_id, FilterUpdate(**payload.model_dump()), db)
    except ValueError as e:
        existing = await get_filter(user.id, filter_id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": existing, "error": str(e)})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx, status_code=422)
    if save_and_test:
        updated = await get_filter(user.id, filter_id, db)
        test_result = await test_filter(user.id, filter_id, db)
        ctx = await _filter_form_context(user, db)
        ctx.update({"filter": updated, "test_result": test_result})
        return templates.TemplateResponse(request, "settings/filter_edit.html", ctx)
    return RedirectResponse("/settings/filters", status_code=303)


@router.delete("/filters/{filter_id}", response_class=HTMLResponse)
async def settings_filter_delete(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_filter(user.id, filter_id, db)
    filters = await list_filters(user.id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/filters_list.html", {
        "filters": filters,
        "labels": labels,
    })


@router.post("/filters/{filter_id}/test", response_class=HTMLResponse)
async def settings_filter_test(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await test_filter(user.id, filter_id, db)
    if not result:
        return HTMLResponse("<p class='text-red-500'>Filter not found.</p>", status_code=404)
    return templates.TemplateResponse(request, "settings/partials/filter_test_result.html", {
        "result": result,
    })


@router.post("/filters/{filter_id}/apply", response_class=HTMLResponse)
async def settings_filter_apply(
    filter_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    matched, changed = await apply_filter_retroactively(user.id, filter_id, db)
    return templates.TemplateResponse(request, "settings/partials/filter_apply_result.html", {
        "matched": matched,
        "changed": changed,
    })


# ── Form parsing helpers ──────────────────────────────────────────────────────

def _parse_filter_form(form) -> FilterCreate:
    """Parse multi-value filter form into FilterCreate."""
    conditions = []
    fields = form.getlist("cond_field")
    operators = form.getlist("cond_operator")
    values = form.getlist("cond_value")
    positions = form.getlist("cond_position")
    for i, (field, op, val) in enumerate(zip(fields, operators, values)):
        val = val.strip()
        if field and op and val:
            conditions.append(FilterConditionCreate(
                field=field, operator=op, value=val,
                position=int(positions[i]) if i < len(positions) else i,
            ))

    actions = []
    action_types = form.getlist("action_type")
    action_values = form.getlist("action_value")
    for a_type, a_val in zip(action_types, action_values):
        if a_type:
            actions.append(FilterActionCreate(
                action_type=a_type,
                action_value=a_val or None,
            ))

    scope_include = [v for v in form.getlist("scope_include") if v]
    scope_except = [v for v in form.getlist("scope_except") if v]

    return FilterCreate(
        name=form.get("name", ""),
        is_active=form.get("is_active") == "true",
        match_operator=form.get("match_operator", "AND"),
        position=safe_int(form.get("position"), 0),
        stop_on_match=form.get("stop_on_match") == "true",
        scope_include=scope_include,
        scope_except=scope_except,
        conditions=conditions,
        actions=actions,
    )


# ── API Tokens ────────────────────────────────────────────────────────────────

async def _list_tokens(user_id: int, db: AsyncSession) -> list[ApiToken]:
    result = await db.execute(
        select(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at == None)
        .order_by(ApiToken.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/tokens", response_class=HTMLResponse)
async def settings_tokens(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tokens = await _list_tokens(user.id, db)
    return templates.TemplateResponse(request, "settings/tokens.html", {"tokens": tokens})


@router.post("/tokens", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_api_tokens)
async def settings_tokens_create(
    request: Request,
    name: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = name.strip()
    if not name:
        tokens = await _list_tokens(user.id, db)
        return templates.TemplateResponse(request, "settings/tokens.html", {
            "tokens": tokens,
            "error": "Token name cannot be empty.",
        }, status_code=422)

    raw_token = secrets.token_urlsafe(32)
    token_prefix = raw_token[:8]
    token_hash = hash_token(raw_token)

    db.add(ApiToken(
        user_id=user.id,
        name=name,
        token_hash=token_hash,
        token_prefix=token_prefix,
    ))
    await db.commit()

    tokens = await _list_tokens(user.id, db)
    return templates.TemplateResponse(request, "settings/tokens.html", {
        "tokens": tokens,
        "new_token": raw_token,
        "new_token_name": name,
    })


@router.post("/tokens/{token_id}/revoke", response_class=HTMLResponse)
async def settings_tokens_revoke(
    token_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(ApiToken).where(ApiToken.id == token_id, ApiToken.user_id == user.id)
    )
    token = result.scalar_one_or_none()
    if token and token.revoked_at is None:
        token.revoked_at = datetime.now(timezone.utc)
        await db.commit()

    tokens = await _list_tokens(user.id, db)
    return templates.TemplateResponse(request, "settings/partials/tokens_list.html", {"tokens": tokens})


# ── Profile ───────────────────────────────────────────────────────────────────

@router.get("/profile", response_class=HTMLResponse)
async def settings_profile(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(request, "settings/profile.html", {"user": user})


@router.post("/profile/name", response_class=HTMLResponse)
async def settings_profile_name(
    request: Request,
    display_name: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    display_name = display_name.strip()
    if not display_name:
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "name_error": "Display name cannot be empty.",
        })
    user.display_name = display_name
    await db.commit()
    return templates.TemplateResponse(request, "settings/profile.html", {
        "user": user,
        "name_saved": True,
    })


@router.post("/profile/email", response_class=HTMLResponse)
async def settings_profile_email(
    request: Request,
    email: str = Form(...),
    current_password: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    email = email.strip().lower()
    if not email:
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "Email cannot be empty.",
        })
    if not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "Current password is incorrect.",
        })
    existing = await db.execute(
        select(User).where(User.email == email, User.id != user.id)
    )
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "This email is already in use.",
        })
    user.email = email
    await db.commit()
    return templates.TemplateResponse(request, "settings/profile.html", {
        "user": user,
        "email_saved": True,
    })


@router.post("/profile/password", response_class=HTMLResponse)
async def settings_profile_password(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    current = form.get("current_password", "")
    new_pw = form.get("new_password", "")
    confirm = form.get("confirm_password", "")

    if not verify_password(current, user.password_hash):
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "pw_error": "Current password is incorrect.",
        })
    if len(new_pw) < 8:
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "pw_error": "New password must be at least 8 characters.",
        })
    if new_pw != confirm:
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "pw_error": "Passwords do not match.",
        })

    user.password_hash = hash_password(new_pw)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    await db.commit()
    return templates.TemplateResponse(request, "settings/profile.html", {
        "user": user,
        "pw_saved": True,
    })


# ── Preferences ───────────────────────────────────────────────────────────────

_DENSITY_VALUES = {"compact", "comfortable", "summary"}
_SORT_VALUES = {"newest", "oldest"}
_FONT_SIZE_VALUES = {"sm", "md", "lg"}
_FONT_FAMILY_VALUES = {"sans", "serif"}


async def _get_or_create_settings(user: User, db: AsyncSession) -> UserSettings:
    result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    s = result.scalar_one_or_none()
    if s is None:
        s = UserSettings(user_id=user.id)
        db.add(s)
        await db.flush()
    return s


@router.get("/preferences", response_class=HTMLResponse)
async def settings_preferences(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    s = await _get_or_create_settings(user, db)
    return templates.TemplateResponse(request, "settings/preferences.html", {"s": s})


@router.post("/preferences", response_class=HTMLResponse)
async def settings_preferences_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    s = await _get_or_create_settings(user, db)

    density_web = form.get("list_density_web", "comfortable")
    if density_web not in _DENSITY_VALUES:
        density_web = "comfortable"
    s.list_density_web = density_web

    density_mobile = form.get("list_density_mobile", "compact")
    if density_mobile not in _DENSITY_VALUES:
        density_mobile = "compact"
    s.list_density_mobile = density_mobile

    sort_order = form.get("default_sort_order", "newest")
    if sort_order not in _SORT_VALUES:
        sort_order = "newest"
    s.default_sort_order = sort_order

    unread_filter = form.get("unread_filter", "adaptive")
    if unread_filter not in {"show_all", "unread_only", "adaptive"}:
        unread_filter = "adaptive"
    s.unread_filter = unread_filter

    s.mark_read_on_scroll = form.get("mark_read_on_scroll") == "on"

    label_display = form.get("label_display", "indicator")
    if label_display not in {"none", "indicator", "dots"}:
        label_display = "indicator"
    s.label_display = label_display

    articles_per_page = safe_int(form.get("articles_per_page"), 50)
    if articles_per_page is not None:
        s.articles_per_page = max(10, min(200, articles_per_page))

    bucket_small_max = safe_int(form.get("bucket_small_max"), 640)
    bucket_medium_max = safe_int(form.get("bucket_medium_max"), 1100)
    if bucket_small_max is not None and bucket_medium_max is not None:
        bucket_small_max = max(320, min(1000, bucket_small_max))
        bucket_medium_max = max(bucket_small_max + 100, min(2000, bucket_medium_max))
        s.bucket_small_max = bucket_small_max
        s.bucket_medium_max = bucket_medium_max

    font_size = form.get("reading_font_size", "md")
    if font_size not in _FONT_SIZE_VALUES:
        font_size = "md"
    s.reading_font_size = font_size

    font_family = form.get("reading_font_family", "sans")
    if font_family not in _FONT_FAMILY_VALUES:
        font_family = "sans"
    s.reading_font_family = font_family

    await db.commit()
    return templates.TemplateResponse(request, "settings/preferences.html", {
        "s": s,
        "saved": True,
    })


# ── OPML ──────────────────────────────────────────────────────────────────────

@router.get("/opml", response_class=HTMLResponse)
async def settings_opml(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(request, "settings/opml.html", {})


@router.get("/opml/export")
async def settings_opml_export(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    xml = await export_opml(user, db)
    filename = f"readfine-{datetime.now(timezone.utc).strftime('%Y%m%d')}.opml"
    return Response(
        content=xml.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/opml/import", response_class=HTMLResponse)
async def settings_opml_import(
    request: Request,
    file: UploadFile = File(...),
    import_feeds: bool = Form(False),
    import_labels: bool = Form(False),
    import_prefs: bool = Form(False),
    import_filters: bool = Form(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(request, "settings/opml.html", {
            "error": "File too large (max 1 MB).",
        })

    try:
        result = await import_opml(
            user=user,
            xml_bytes=raw,
            import_feeds=import_feeds,
            import_labels=import_labels,
            import_prefs=import_prefs,
            import_filters=import_filters,
            db=db,
        )
    except ValueError as exc:
        return templates.TemplateResponse(request, "settings/opml.html", {
            "error": str(exc),
        })

    return templates.TemplateResponse(request, "settings/opml.html", {
        "import_result": result,
    })


# ── AI settings ───────────────────────────────────────────────────────────────

async def _ai_page_context(user: User, db: AsyncSession) -> dict:
    s = await _get_or_create_settings(user, db)
    keys = await list_api_keys(user.id, db)
    cost = await estimate_monthly_cost(user.id, db)
    return {
        "s": s,
        "keys": keys,
        "cost": cost,
        "providers": SUPPORTED_PROVIDERS,
        "provider_docs": PROVIDER_DOCS_URLS,
    }


@router.get("/ai", response_class=HTMLResponse)
async def settings_ai(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ctx = await _ai_page_context(user, db)
    return templates.TemplateResponse(request, "settings/ai.html", ctx)


@router.post("/ai/keys", response_class=HTMLResponse)
async def settings_ai_keys_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    provider = (form.get("provider") or "").strip()
    api_key = (form.get("api_key") or "").strip()

    if provider not in SUPPORTED_PROVIDERS:
        ctx = await _ai_page_context(user, db)
        ctx["keys_error"] = "Unknown provider."
        return templates.TemplateResponse(request, "settings/ai.html", ctx)

    if api_key:
        await save_api_key(user.id, provider, api_key, db)
        ctx = await _ai_page_context(user, db)
        ctx["keys_saved"] = provider
    else:
        await delete_api_key(user.id, provider, db)
        ctx = await _ai_page_context(user, db)
        ctx["keys_deleted"] = provider

    return templates.TemplateResponse(request, "settings/ai.html", ctx)


@router.post("/ai/preferences", response_class=HTMLResponse)
async def settings_ai_preferences_save(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    s = await _get_or_create_settings(user, db)

    s.ai_fast_provider = (form.get("ai_fast_provider") or "").strip() or None
    s.ai_fast_model = (form.get("ai_fast_model") or "").strip() or None
    s.ai_quality_provider = (form.get("ai_quality_provider") or "").strip() or None
    s.ai_quality_model = (form.get("ai_quality_model") or "").strip() or None
    s.ai_preference_text = (form.get("ai_preference_text") or "").strip() or None
    s.ai_scoring_enabled_default = form.get("ai_scoring_enabled_default") == "on"
    s.ai_summary_enabled_default = form.get("ai_summary_enabled_default") == "on"

    await db.commit()
    ctx = await _ai_page_context(user, db)
    ctx["prefs_saved"] = True
    return templates.TemplateResponse(request, "settings/ai.html", ctx)


@router.post("/ai/verify/{slot}", response_class=HTMLResponse)
async def settings_ai_verify(
    slot: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if slot not in ("fast", "quality"):
        return HTMLResponse("Invalid slot", status_code=400)

    form = await request.form()
    provider_override = (form.get(f"ai_{slot}_provider") or "").strip() or None
    model_override = (form.get(f"ai_{slot}_model") or "").strip() or None
    result = await verify_ai_slot(user.id, slot, db, provider_override, model_override)
    if result["ok"]:
        html = (
            f'<span class="text-green-600 text-sm">✓ Connected — {result["model"]}</span>'
        )
    else:
        html = (
            f'<span class="text-red-600 text-sm">✗ {result["error"]}</span>'
        )
    return HTMLResponse(html)


@router.post("/ai/generate-preference", response_class=HTMLResponse)
async def settings_ai_generate_preference(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    client, provider, model = await get_ai_client(user.id, "fast", db)
    if client is None:
        return HTMLResponse(
            '<span class="text-red-600 text-sm">Fast model not configured.</span>'
        )
    try:
        text_result = await generate_preference_text(user.id, db, client, provider, model)
    except Exception as exc:
        logger.warning("generate_preference_text failed for user=%s: %s", user.id, exc)
        return HTMLResponse(
            f'<span class="text-red-600 text-sm">Error: {str(exc)[:150]}</span>'
        )

    escaped = text_result.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    return HTMLResponse(
        f'<span class="text-green-600 text-sm">Generated — review and save below.</span>'
        f'<textarea name="ai_preference_text" id="ai_preference_text" rows="4"'
        f' class="w-full border border-gray-300 rounded px-3 py-2 text-sm font-mono"'
        f' hx-swap-oob="true">{escaped}</textarea>'
    )

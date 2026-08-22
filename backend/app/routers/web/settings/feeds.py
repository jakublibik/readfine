"""Web routes for feed subscription, testing, editing, and listing in settings."""
import asyncio
import logging
from datetime import datetime, timezone

import feedparser
import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.fetcher.failure import clear_failure_state
from app.fetcher.interval import auto_interval_min
from app.fetcher.scheduler import compute_next_fetch_at
from app.models.feed import Folder, UserFeed
from app.models.settings import AppSettings
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.services.feed import cache_feed_preview, may_edit_feed_auth, subscribe, unsubscribe
from app.templating import templates
from app.utils.crypto import auth_pair, encrypt
from app.utils.feed_detect import detect_feeds
from app.utils.http_client import READFINE_UA
from app.utils.parsing import safe_int
from app.utils.url_validator import (
    async_validate_feed_url,
    fetch_url_page,
    format_retry_in,
    rate_limited_until,
    redact_url,
    split_url_credentials,
)

from .common import _ai_selector_available, _ensure_scheme, _get_feeds_context, _snap_interval

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/feeds", response_class=HTMLResponse)
async def settings_feeds(
    request: Request,
    added: str = "",
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    return templates.TemplateResponse(request, "settings/feeds.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
        "error": None,
        "subscribe_url": "",
        "detected_feeds": [],
        "added": added,
        "purge_days": app_s.default_purge_after_days if app_s else None,
        "purge_count": app_s.default_purge_keep_count if app_s else None,
        "default_fetch_interval_min": (app_s.default_fetch_interval_min if app_s else None) or 60,
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
    url = _ensure_scheme(url)
    if not url:
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": "Please enter a URL."})
    auth_user = fetch_auth_user.strip() or None
    auth_pass = fetch_auth_pass or None
    # Same split Subscribe will do, so the test says the same thing the subscribe will
    # find: with the credentials left in the address the "are these needed?" check
    # never runs, and the parse would be cached under an address Subscribe no longer uses.
    url, url_auth_user, url_auth_pass = split_url_credentials(url)
    if url_auth_user is not None and not auth_user and not auth_pass:
        auth_user, auth_pass = url_auth_user, url_auth_pass

    try:
        await async_validate_feed_url(url)
    except ValueError as e:
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": str(e)})

    _headers = {
        "User-Agent": READFINE_UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    }
    # The test has to try exactly what the fetch will, hence the shared rule.
    auth = auth_pair(auth_user, auth_pass)
    has_auth = auth is not None
    loop = asyncio.get_running_loop()

    async def _fetch(with_auth):
        """Returns (page, error_string). Uses SSRF-safe redirect loop."""
        fetch_auth = auth if with_auth else None
        try:
            page = await loop.run_in_executor(
                None,
                lambda: fetch_url_page(url, auth=fetch_auth, timeout=15, headers=_headers),
            )
            return page, None
        except httpx.HTTPStatusError as e:
            sc = e.response.status_code
            if sc == 403:
                return None, "HTTP 403: Access denied. The server is likely blocking requests from this host (geo-block or datacenter IP block)."
            return None, f"HTTP {sc}: {e.response.reason_phrase}"
        except (httpx.RequestError, ValueError) as e:
            return None, f"Connection error: {e}"

    # Always fetch with the configured auth (or no auth if none provided)
    page, error = await _fetch(with_auth=True)

    auth_status = None  # will be set when credentials were provided
    if has_auth and page is None and error and "401" in error:
        # Credentials provided but got 401 → wrong credentials
        auth_status = "wrong"
    elif has_auth and page is not None:
        # Succeeded with auth — check if auth was actually needed
        no_auth_page, no_auth_error = await _fetch(with_auth=False)
        if no_auth_page is not None:
            auth_status = "not_required"
        else:
            auth_status = "required_ok"

    if error and auth_status != "wrong":
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": error})

    if auth_status == "wrong":
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html",
                                          {"error": f"{error} — credentials rejected"})

    parsed = await loop.run_in_executor(None, feedparser.parse, page.text)

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

    original_title = (parsed.feed.get("title") or "").strip() or None
    feed_title = original_title or url
    entry_count = len(parsed.entries)
    # Cache this parse so a follow-up Subscribe reuses it instead of re-fetching
    # (single network request per add — important for rate-limited sites). Public
    # feeds only; auth'd fetches are user-specific and not shared.
    if not has_auth:
        cache_feed_preview(url, parsed, page.permanent_url)
    return templates.TemplateResponse(request, "settings/partials/feed_test_result.html", {
        "feed_title": feed_title,
        # Only the feed's real title, never the URL fallback — the subscribe form uses it
        # as the "Custom title" placeholder, where a URL would be nonsense.
        "original_title": original_title,
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
    url = _ensure_scheme(url)
    custom_title = form.get("custom_title", "").strip() or None
    folder_id_raw = form.get("folder_id")
    folder_id = safe_int(folder_id_raw)
    fetch_auth_user = form.get("fetch_auth_user", "").strip() or None
    fetch_auth_pass = form.get("fetch_auth_pass", "") or None
    is_private = form.get("is_private") == "on"
    # Import scope: "recent" (default) bounds to the retention horizon; "latest"
    # imports up to import_limit newest articles regardless of age (e.g. archive import).
    import_mode = "latest" if form.get("import_mode") == "latest" else "recent"
    import_limit = max(1, min(safe_int(form.get("import_limit")) or 500, 100000))
    interval_raw = safe_int(form.get("fetch_interval_min"))
    fetch_interval_min = _snap_interval(interval_raw) if interval_raw else None

    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    error = None
    detected_feeds = []
    try:
        uf = await subscribe(user=user, url=url, folder_id=folder_id,
                             custom_title=custom_title, fetch_auth_user=fetch_auth_user,
                             fetch_auth_pass=fetch_auth_pass, is_private=is_private, db=db,
                             import_mode=import_mode, import_limit=import_limit,
                             fetch_interval_min=fetch_interval_min)
        from urllib.parse import quote
        redirect_url = f"/settings/feeds?added={quote(uf.feed.title)}"
        if request.headers.get("HX-Request"):
            return Response(headers={"HX-Redirect": redirect_url})
        return RedirectResponse(redirect_url, status_code=303)
    except ValueError as e:
        error = str(e)
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
            error = "Access denied (403). The server is likely blocking requests from this host (geo-block or datacenter IP block)."
        elif status == 401:
            error = "Authentication required (401). Try adding HTTP credentials."
        elif status == 429:
            # Reads Retry-After and x-ratelimit-reset (Reddit sends the latter, no
            # Retry-After). Resets are often seconds, so show seconds under ~90s.
            now = datetime.now(timezone.utc)
            until = rate_limited_until(e.response.headers, now)
            if until is not None:
                error = (f"Too many requests (429) — the server is rate-limiting this host. "
                         f"Try again in {format_retry_in(until, now)}.")
            else:
                error = ("Too many requests (429) — the server is rate-limiting this host. "
                         "Try again in a few minutes.")
        elif status in (500, 502, 503, 504):
            error = (f"The feed server returned an error ({status}). "
                     "It may be temporarily down — try again later.")
        else:
            error = f"HTTP error {status} when fetching the feed."
        try:
            detected_feeds = await detect_feeds(url)
        except Exception:
            pass
    except httpx.TimeoutException:
        error = ("The feed server took too long to respond (timeout). "
                 "It may be temporarily down or slow — try again later.")
    except httpx.TransportError as e:
        # Connection dropped / refused before any HTTP status (e.g. RemoteProtocolError
        # "Server disconnected without sending a response"). CDNs like Cloudflare do
        # this to throttle datacenter IPs instead of returning a 429, so distinguish it
        # from a bad URL.
        logger.warning("Transport error during feed subscribe (url=%s): %s", redact_url(url), e)
        error = ("The feed server closed the connection without responding — it is likely "
                 "blocking or rate-limiting requests from this host (common for datacenter "
                 "IPs). Try again later.")
    except Exception as e:
        logger.error("Unexpected error during feed subscribe (url=%s): %s", redact_url(url), e)
        error = "Could not subscribe to feed. Please check the URL and try again."

    is_rss_error = error and any(k in error for k in ("valid RSS", "valid feed", "Not a valid", "parse", "404", "403", "HTTP error"))
    show_scrape_option = (
        is_rss_error
        and not detected_feeds
        and url.startswith(("http://", "https://"))
    )

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "settings/partials/feed_test_result.html", {
            "error": error if not detected_feeds else None,
            "detected_feeds": detected_feeds,
            "scrape_available": show_scrape_option,
            "test_url": url,
        })

    # Non-HTMX fallback (no-JS)
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/feeds.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
        "error": error if not detected_feeds else None,
        "subscribe_url": url,
        "detected_feeds": detected_feeds,
        "show_scrape_option": show_scrape_option,
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
    user_s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    ai_selector_available = _ai_selector_available(app_s, user_s)
    is_sole_subscriber = uf.feed.subscriber_count == 1
    default_interval = (app_s.default_fetch_interval_min if app_s else None) or 60
    min_interval = (app_s.min_fetch_interval_min if app_s else None) or 15
    max_interval = (app_s.max_fetch_interval_min if app_s else None) or 360
    return templates.TemplateResponse(request, "settings/feed_edit.html", {
        "uf": uf,
        "folders": folders,
        "next_fetch_at": compute_next_fetch_at(
            uf.feed,
            default_interval_min=default_interval,
            min_interval_min=min_interval,
            max_interval_min=max_interval,
        ),
        "is_sole_subscriber": is_sole_subscriber,
        "can_edit_interval": user.role == "admin" or uf.feed.is_private or is_sole_subscriber,
        # Same function the POST handler gates on, so the form cannot offer a field the
        # save would then ignore.
        "can_edit_auth": may_edit_feed_auth(uf.feed),
        "default_interval_min": default_interval,
        # Effective interval Auto would use for this feed — hint next to the "Auto" option.
        "auto_interval_min": auto_interval_min(
            uf.feed.derived_interval_min, default_interval_min=default_interval,
            min_interval_min=min_interval, max_interval_min=max_interval,
        ),
        "ai_summary_global_enabled": bool(user_s and user_s.ai_summary_enabled_default),
        "ai_selector_available": ai_selector_available,
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
    uf.readable_auto_disabled_reason = None
    if form.get("ai_summary_enabled_present") == "1":
        uf.ai_summary_enabled = form.get("ai_summary_enabled") == "on"

    # Interval is feed-wide. Only let the user change it when the feed is
    # effectively theirs (private or sole subscriber) or they're an admin;
    # on a shared public feed it's read-only (see feed_edit.html).
    if user.role == "admin" or uf.feed.is_private or uf.feed.subscriber_count == 1:
        interval_raw = safe_int(form.get("fetch_interval_min"))
        if interval_raw is not None:
            uf.feed.fetch_interval_min = _snap_interval(interval_raw)
        else:
            uf.feed.fetch_interval_min = None

    # Unlike the interval above, credentials are a sole subscriber's to change; see
    # services.feed.may_edit_feed_auth for why, and feed_edit.html, which hides the
    # fields under the same rule and tells a shared feed's subscriber how to get a
    # credentialed copy of their own.
    if may_edit_feed_auth(uf.feed):
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

    # Saving the form is the subscriber's way of saying "try this again": it switches a
    # stopped feed back on and drops the whole failure trail, deferral included. Clearing
    # only the counters used to leave a feed active but deferred by the block backoff for
    # up to a day, with the manual refresh button answering "rate-limited" as well.
    clear_failure_state(uf.feed)
    # Saving the form clears readable_auto_disabled above, which takes the feed out of
    # the revival job's reach, so drop its bookkeeping too: a scheduled probe would
    # otherwise linger on a feed the user has just decided about, and the spent-attempt
    # count would keep the feed barred from future probes forever. The revival timestamp
    # goes with them, or the admin panel would keep listing a past revival next to the
    # attempt count we just zeroed, which reads as a feed revived by no probe at all.
    uf.feed.readable_revival_next_at = None
    uf.feed.readable_revival_attempts = 0
    uf.feed.readable_revived_at = None
    if uf.extract_readable:
        # Start the auto-disable streaks from here, so a feed the user has just turned
        # extraction back on for is not condemned by the 403s that got it turned off.
        # Done on every save that leaves extraction on, not only on a re-enable: the
        # lines above already treat saving the form as a clean slate for the error
        # counters, and this is the same slate.
        from app.services.readable_service import stamp_readable_streak_start
        await stamp_readable_streak_start(uf.feed_id, db)

    await db.commit()
    return RedirectResponse("/settings/feeds", status_code=303)


@router.delete("/feeds/{user_feed_id}", response_class=HTMLResponse)
async def settings_feed_delete(
    user_feed_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    cleanup = None
    try:
        cleanup = await unsubscribe(user, user_feed_id, db)
    except ValueError:
        pass
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
        "scope_cleanup": cleanup,
    })


@router.get("/feeds-list", response_class=HTMLResponse)
async def settings_feeds_list(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
    })

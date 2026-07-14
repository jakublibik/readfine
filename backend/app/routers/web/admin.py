"""Web routes for the admin panel."""
import asyncio
import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.database import get_db
from app.fetcher import host_throttle
from app.fetcher.interval import auto_interval_min
from app.fetcher.scheduler import compute_next_fetch_at
from app.models.user import User
from app.services.host_rate_limit_service import flush as flush_host_rate_limits
from app.services.admin_service import (
    clear_feed_error,
    create_invitation,
    delete_feed,
    delete_user,
    get_app_settings,
    get_dashboard_stats,
    get_feed,
    group_feeds_by_host,
    list_audit_logs,
    list_feeds_with_stats,
    list_fetch_logs,
    list_invitations,
    list_users,
    log_audit,
    revoke_invitation,
    toggle_feed_pause,
    toggle_user_active,
    update_app_settings,
    update_feed_admin,
)
from app.utils.crypto import encrypt
from app.utils.datetime_format import format_until
from app.utils.parsing import clamp, safe_int
from app.utils.smtp import send_email

logger = logging.getLogger(__name__)

from app.templating import templates, set_ai_enabled, set_feedback_available

router = APIRouter(prefix="/admin", tags=["admin"])


def _quantize15(val: int | None, default: int) -> int:
    """Round val to the nearest multiple of 15, clamped to [15, 1440]."""
    v = val if val is not None else default
    return max(15, min(1440, round(v / 15) * 15))


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app import __version__
    stats = await get_dashboard_stats(db)
    return templates.TemplateResponse(
        request, "admin/dashboard.html", {"stats": stats, "app_version": __version__}
    )


@router.get("/scoring-eval", response_class=HTMLResponse)
async def admin_scoring_eval(
    request: Request,
    days: int | None = None,
    user_id: str | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services.ai_eval_service import get_scoring_eval
    window = clamp(days, 7, 365, 90)
    users = await list_users(db)
    eval_data = await get_scoring_eval(db, days=window, user_id=safe_int(user_id))
    return templates.TemplateResponse(request, "admin/scoring_eval.html", {
        "eval": eval_data,
        "users": users,
    })


# ── Users ─────────────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    users = await list_users(db)
    return templates.TemplateResponse(request, "admin/users.html", {
        "users": users,
        "current_user": user,
    })


@router.post("/users/{user_id}/activate", response_class=HTMLResponse)
async def admin_toggle_active(
    user_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    target = await toggle_user_active(db, user_id, admin_id=user.id)
    if target:
        action = "user_activate" if target.is_active else "user_deactivate"
        await log_audit(db, user.id, action, target_type="user", target_id=target.id)
    users = await list_users(db)
    return templates.TemplateResponse(request, "admin/partials/users_table.html", {
        "users": users,
        "current_user": user,
    })


@router.delete("/users/{user_id}", response_class=HTMLResponse)
async def admin_delete_user(
    user_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_user(db, user_id, admin_id=user.id)
    if deleted:
        await log_audit(db, user.id, "user_delete", target_type="user", target_id=user_id)
    users = await list_users(db)
    return templates.TemplateResponse(request, "admin/partials/users_table.html", {
        "users": users,
        "current_user": user,
    })


# ── App Settings ──────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = await get_app_settings(db)
    legal_configured = bool(s.legal_operator_name and s.legal_contact_email and s.legal_jurisdiction)
    return templates.TemplateResponse(request, "admin/settings.html", {
        "s": s,
        "saved": False,
        "error": None,
        "legal_configured": legal_configured,
    })


@router.post("/settings", response_class=HTMLResponse)
async def admin_settings_save(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    s = await get_app_settings(db)

    smtp_password_plain = form.get("smtp_password", "").strip()

    # Retention horizon: presets only (max 120 keeps the T1 < T2 invariant; T2 = 180).
    # Fall back to the current value on an out-of-range / tampered POST.
    _purge_days = safe_int(form.get("default_purge_after_days"))
    if _purge_days not in (30, 60, 90, 120):
        _purge_days = s.default_purge_after_days or 60

    data = {
        "registration_enabled": form.get("registration_enabled") == "true",
        "default_fetch_interval_min": _quantize15(safe_int(form.get("default_fetch_interval_min")), 60),
        "min_fetch_interval_min": _quantize15(safe_int(form.get("min_fetch_interval_min")), 15),
        # Cap for the adaptive interval; never below the minimum (else the read-time clamp inverts).
        "max_fetch_interval_min": max(
            _quantize15(safe_int(form.get("max_fetch_interval_min")), 360),
            _quantize15(safe_int(form.get("min_fetch_interval_min")), 15),
        ),
        "max_feeds_per_user": clamp(safe_int(form.get("max_feeds_per_user")), 1, 9999, 200),
        "default_purge_after_days": _purge_days,
        "smtp_host": form.get("smtp_host", "").strip() or None,
        "smtp_port": clamp(safe_int(form.get("smtp_port")), 1, 65535, 587),
        "smtp_user": form.get("smtp_user", "").strip() or None,
        "smtp_from_email": form.get("smtp_from_email", "").strip() or None,
        "smtp_use_tls": form.get("smtp_use_tls") == "true",
        "ai_enabled": form.get("ai_enabled") == "true",
        "feedback_enabled": form.get("feedback_enabled") == "true",
        "legal_operator_name": form.get("legal_operator_name", "").strip() or None,
        "legal_contact_email": form.get("legal_contact_email", "").strip() or None,
        "legal_jurisdiction": form.get("legal_jurisdiction", "").strip() or None,
    }
    if (
        data["legal_operator_name"] != s.legal_operator_name
        or data["legal_contact_email"] != s.legal_contact_email
        or data["legal_jurisdiction"] != s.legal_jurisdiction
    ):
        data["legal_last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # Only update SMTP password if a new value was provided
    if smtp_password_plain:
        data["smtp_password_encrypted"] = encrypt(smtp_password_plain)

    try:
        s = await update_app_settings(db, data)
        set_ai_enabled(s.ai_enabled)
        set_feedback_available(bool(s.feedback_enabled and s.smtp_host and s.smtp_from_email))
        await log_audit(db, user.id, "app_settings_update", target_type="app_settings", target_id=1)
        legal_configured = bool(s.legal_operator_name and s.legal_contact_email and s.legal_jurisdiction)
        return templates.TemplateResponse(request, "admin/settings.html", {
            "s": s,
            "saved": True,
            "error": None,
            "legal_configured": legal_configured,
        })
    except Exception as e:
        logger.error("Failed to save app settings: %s", e)
        s = await get_app_settings(db)
        legal_configured = bool(s.legal_operator_name and s.legal_contact_email and s.legal_jurisdiction)
        return templates.TemplateResponse(request, "admin/settings.html", {
            "s": s,
            "saved": False,
            "error": "Failed to save settings.",
            "legal_configured": legal_configured,
        }, status_code=500)


# ── SMTP test ─────────────────────────────────────────────────────────────────

@router.post("/settings/test-smtp", response_class=HTMLResponse)
async def admin_test_smtp(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    saved = await get_app_settings(db)

    # Build a temporary settings object from form values;
    # fall back to saved values for anything not in the form.
    s = SimpleNamespace(
        smtp_host=form.get("smtp_host", "").strip() or saved.smtp_host,
        smtp_port=safe_int(form.get("smtp_port"), saved.smtp_port or 587),
        smtp_user=form.get("smtp_user", "").strip() or saved.smtp_user,
        smtp_from_email=form.get("smtp_from_email", "").strip() or saved.smtp_from_email,
        smtp_use_tls=form.get("smtp_use_tls") == "true",
        # Use new password if provided, otherwise keep saved encrypted password
        smtp_password_encrypted=(
            encrypt(form["smtp_password"].strip())
            if form.get("smtp_password", "").strip()
            else saved.smtp_password_encrypted
        ),
    )

    try:
        await asyncio.to_thread(
            send_email, s, user.email,
            "Readfine – SMTP test",
            f"This is a test email sent from Readfine admin panel to {user.email}.",
        )
        from html import escape
        return HTMLResponse(
            '<span class="text-green-600">Test email sent to ' + escape(user.email) + ".</span>"
        )
    except ValueError as e:
        logger.warning("SMTP test – not configured: %s", e)
        return HTMLResponse('<span class="text-red-600">SMTP not configured (missing host or from address).</span>')
    except Exception as e:
        logger.error("SMTP test failed: %s", e)
        from html import escape
        detail = escape(f"{type(e).__name__}: {e}".strip().rstrip(":").strip())
        return HTMLResponse(
            '<span class="text-red-600">Failed to send test email: ' + detail + "</span>"
        )


# ── Invitations ───────────────────────────────────────────────────────────────

@router.get("/invitations", response_class=HTMLResponse)
async def admin_invitations(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    invitations = await list_invitations(db)
    return templates.TemplateResponse(request, "admin/invitations.html", {
        "invitations": invitations,
        "base_url": str(request.base_url).rstrip("/"),
    })


@router.post("/invitations", response_class=HTMLResponse)
async def admin_create_invitation(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    email = form.get("email", "").strip() or None
    expires_str = form.get("expires_at", "").strip()
    expires_at = None
    if expires_str:
        try:
            # Set to end of selected day (23:59:59 UTC) so the invitation stays valid all day
            expires_at = datetime.fromisoformat(expires_str).replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
        except ValueError:
            pass

    inv = await create_invitation(db, admin_id=user.id, email=email, expires_at=expires_at)
    await log_audit(db, user.id, "invitation_create", target_type="invitation", target_id=inv.id,
                    detail={"email": email})
    invitations = await list_invitations(db)
    return templates.TemplateResponse(request, "admin/partials/invitations_table.html", {
        "invitations": invitations,
        "base_url": str(request.base_url).rstrip("/"),
    })


@router.delete("/invitations/{invitation_id}", response_class=HTMLResponse)
async def admin_revoke_invitation(
    invitation_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    inv = await revoke_invitation(db, invitation_id)
    if inv:
        await log_audit(db, user.id, "invitation_revoke", target_type="invitation", target_id=invitation_id)
    invitations = await list_invitations(db)
    return templates.TemplateResponse(request, "admin/partials/invitations_table.html", {
        "invitations": invitations,
        "base_url": str(request.base_url).rstrip("/"),
    })


# ── Feeds ─────────────────────────────────────────────────────────────────────

def _norm_group(group: str | None) -> str:
    return "host" if group == "host" else "az"


async def _feeds_context(db, group: str = "az") -> dict:
    """Feeds list annotated for the admin table: predicted next fetch for errored
    feeds (mirrors settings) and the effective default interval for feeds without a
    per-feed override. When ``group == "host"`` the rows are also grouped by fetch
    host for the admin table's grouped view."""
    feeds = await list_feeds_with_stats(db)
    s = await get_app_settings(db)
    default_interval = (s.default_fetch_interval_min or 60)
    min_interval = (s.min_fetch_interval_min or 15)
    max_interval = (s.max_fetch_interval_min or 360)
    now = datetime.now(timezone.utc)
    for item in feeds:
        f = item["feed"]
        # Predicted next fetch for every scheduled feed (None for paused/disabled/no-subs),
        # shown under "Last fetch" as a relative hint.
        f.next_fetch_at = compute_next_fetch_at(
            f, default_interval_min=default_interval,
            min_interval_min=min_interval, max_interval_min=max_interval, now=now,
        )
        f.next_fetch_rel = format_until(f.next_fetch_at, now)
        # Effective Auto interval the scheduler would use (capped derived value, or the
        # uncapped default fallback), so the table matches behaviour, not the raw stored value.
        f.auto_interval_min = auto_interval_min(
            f.derived_interval_min, default_interval_min=default_interval,
            min_interval_min=min_interval, max_interval_min=max_interval,
        )
    group = _norm_group(group)
    return {
        "feeds": feeds,
        "feed_groups": group_feeds_by_host(feeds) if group == "host" else None,
        "group_mode": group,
        "default_interval_min": max(default_interval, min_interval),
        "has_rate_limits": bool(host_throttle.all_spacing()),
    }


@router.get("/feeds", response_class=HTMLResponse)
async def admin_feeds(
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    return templates.TemplateResponse(request, "admin/feeds.html", await _feeds_context(db, group))


@router.get("/feeds/table", response_class=HTMLResponse)
async def admin_feeds_table(
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Feeds table partial only — used by the A–Z / By-host view toggle."""
    return templates.TemplateResponse(
        request, "admin/partials/feeds_table.html", await _feeds_context(db, group)
    )


# ── Learned per-host rate-limit spacing ───────────────────────────────────────

@router.get("/rate-limits", response_class=HTMLResponse)
async def admin_rate_limits(
    request: Request,
    user: User = Depends(require_admin),
):
    return templates.TemplateResponse(
        request, "admin/partials/rate_limits_modal.html",
        {"spacings": host_throttle.all_spacing(), "now": datetime.now(timezone.utc)},
    )


@router.post("/rate-limits/{host}/clear", response_class=HTMLResponse)
async def admin_clear_rate_limit(
    host: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if host_throttle.clear_spacing(host):
        await flush_host_rate_limits(db)
        await log_audit(db, user.id, "rate_limit_clear", target_type="host", detail={"host": host})
    return templates.TemplateResponse(
        request, "admin/partials/rate_limits_modal.html",
        {"spacings": host_throttle.all_spacing(), "now": datetime.now(timezone.utc)},
    )


# ── Feed actions ──────────────────────────────────────────────────────────────

async def _feeds_response(request: Request, db, user, group: str = "az") -> HTMLResponse:
    return templates.TemplateResponse(
        request, "admin/partials/feeds_table.html", await _feeds_context(db, group)
    )


@router.post("/feeds/{feed_id}/pause", response_class=HTMLResponse)
async def admin_toggle_feed_pause(
    feed_id: int,
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    feed = await toggle_feed_pause(db, feed_id)
    if feed:
        await log_audit(db, user.id, "feed_pause_toggle", target_type="feed", target_id=feed_id,
                        detail={"status": feed.status})
    return await _feeds_response(request, db, user, group)


@router.post("/feeds/{feed_id}/clear-error", response_class=HTMLResponse)
async def admin_clear_feed_error(
    feed_id: int,
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    cleared = await clear_feed_error(db, feed_id)
    if cleared:
        await log_audit(db, user.id, "feed_clear_error", target_type="feed", target_id=feed_id)
    return await _feeds_response(request, db, user, group)


@router.delete("/feeds/{feed_id}", response_class=HTMLResponse)
async def admin_delete_feed(
    feed_id: int,
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_feed(db, feed_id)
    if deleted:
        await log_audit(db, user.id, "feed_delete", target_type="feed", target_id=feed_id)
    return await _feeds_response(request, db, user, group)


@router.get("/feeds/{feed_id}/edit", response_class=HTMLResponse)
async def admin_feed_edit_form(
    feed_id: int,
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Feed-edit form for the admin modal — feed-wide fields only."""
    feed = await get_feed(db, feed_id)
    if not feed:
        return HTMLResponse("<p class='text-red-500 p-4'>Feed not found.</p>", status_code=404)
    s = await get_app_settings(db)
    default_interval = (s.default_fetch_interval_min or 60)
    min_interval = (s.min_fetch_interval_min or 15)
    max_interval = (s.max_fetch_interval_min or 360)
    return templates.TemplateResponse(request, "admin/partials/feed_edit_form.html", {
        "feed": feed,
        "group_mode": _norm_group(group),
        "default_interval_min": default_interval,
        # Effective Auto interval shown as the "Auto (~N min)" hint.
        "auto_interval_min": auto_interval_min(
            feed.derived_interval_min, default_interval_min=default_interval,
            min_interval_min=min_interval, max_interval_min=max_interval,
        ),
    })


@router.post("/feeds/{feed_id}/edit", response_class=HTMLResponse)
async def admin_feed_update(
    feed_id: int,
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    interval_raw = safe_int(form.get("fetch_interval_min"))
    fetch_interval_min = _quantize15(interval_raw, 60) if interval_raw else None
    feed = await update_feed_admin(
        db, feed_id,
        title=form.get("title", ""),
        fetch_interval_min=fetch_interval_min,
        status=form.get("status", ""),
        article_links_selector=form.get("article_links_selector"),
    )
    if not feed:
        return HTMLResponse("<p class='text-red-500 p-4'>Feed not found.</p>", status_code=404)
    await log_audit(db, user.id, "feed_edit", target_type="feed", target_id=feed_id,
                    detail={"title": feed.title, "status": feed.status,
                            "fetch_interval_min": feed.fetch_interval_min})
    resp = await _feeds_response(request, db, user, group)
    # Close the edit modal once the table has been re-rendered.
    resp.headers["HX-Trigger"] = json.dumps({"feedEditDone": True})
    return resp


@router.post("/feeds/{feed_id}/force-fetch", response_class=HTMLResponse)
async def admin_force_fetch(
    feed_id: int,
    request: Request,
    group: str = Query("az"),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    feed = await get_feed(db, feed_id)
    if feed:
        from app.fetcher.rss import cooldown_until
        from app.utils.url_validator import format_retry_in
        now = datetime.now(timezone.utc)
        cd = cooldown_until(feed, now)
        if cd is not None:
            # Known rate-limit window — don't hammer into another 429; the admin
            # button is no override, so surface the wait instead of fetching.
            resp = await _feeds_response(request, db, user, group)
            resp.headers["HX-Trigger"] = json.dumps({"showToast": {
                "msg": f"Rate-limited — try again in {format_retry_in(cd, now)}.",
                "type": "warning",
            }})
            return resp
        if feed.feed_type == "scrape":
            from app.fetcher.scrape import fetch_scrape_feed
            await fetch_scrape_feed(feed, db)
        else:
            from app.fetcher.rss import fetch_feed
            await fetch_feed(feed, db)
        await log_audit(db, user.id, "feed_force_fetch", target_type="feed", target_id=feed_id)
        # A live 429 during the fetch just armed a cooldown but stored the raw
        # httpx error — surface the timed message (only on failure).
        now2 = datetime.now(timezone.utc)
        cd2 = cooldown_until(feed, now2)
        if feed.last_error and cd2 is not None:
            resp = await _feeds_response(request, db, user, group)
            resp.headers["HX-Trigger"] = json.dumps({"showToast": {
                "msg": f"Rate-limited — try again in {format_retry_in(cd2, now2)}.",
                "type": "warning",
            }})
            return resp
    return await _feeds_response(request, db, user, group)


# ── Fetch logs ────────────────────────────────────────────────────────────────

@router.get("/fetch-logs", response_class=HTMLResponse)
async def admin_fetch_logs(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    logs = await list_fetch_logs(db)
    return templates.TemplateResponse(request, "admin/fetch_logs.html", {"logs": logs})


# ── Audit log ─────────────────────────────────────────────────────────────────

@router.get("/audit-log", response_class=HTMLResponse)
async def admin_audit_log(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    logs = await list_audit_logs(db)
    return templates.TemplateResponse(request, "admin/audit_log.html", {"logs": logs})

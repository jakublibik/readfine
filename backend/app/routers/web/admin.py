"""Web routes for the admin panel."""
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.database import get_db
from app.models.user import User
from app.services.admin_service import (
    clear_feed_error,
    create_invitation,
    delete_feed,
    get_app_settings,
    get_dashboard_stats,
    get_feed,
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
)
from app.utils.crypto import encrypt
from app.utils.smtp import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


def _safe_int(value, default=None) -> int | None:
    try:
        return int(value) if value else default
    except (ValueError, TypeError):
        return default


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def admin_dashboard(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    stats = await get_dashboard_stats(db)
    return templates.TemplateResponse(request, "admin/dashboard.html", {"stats": stats})


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


# ── App Settings ──────────────────────────────────────────────────────────────

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    s = await get_app_settings(db)
    return templates.TemplateResponse(request, "admin/settings.html", {
        "s": s,
        "saved": False,
        "error": None,
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

    def _clamp(value: int | None, lo: int, hi: int, default: int) -> int:
        if value is None:
            return default
        return max(lo, min(hi, value))

    data = {
        "registration_enabled": form.get("registration_enabled") == "true",
        "default_fetch_interval_min": _clamp(_safe_int(form.get("default_fetch_interval_min")), 5, 1440, 60),
        "max_feeds_per_user": _clamp(_safe_int(form.get("max_feeds_per_user")), 1, 9999, 200),
        "default_purge_after_days": _clamp(_safe_int(form.get("default_purge_after_days")), 1, 3650, None) if form.get("default_purge_after_days") else None,
        "default_purge_keep_count": _clamp(_safe_int(form.get("default_purge_keep_count")), 1, 100000, None) if form.get("default_purge_keep_count") else None,
        "smtp_host": form.get("smtp_host", "").strip() or None,
        "smtp_port": _clamp(_safe_int(form.get("smtp_port")), 1, 65535, 587),
        "smtp_user": form.get("smtp_user", "").strip() or None,
        "smtp_from_email": form.get("smtp_from_email", "").strip() or None,
        "smtp_use_tls": form.get("smtp_use_tls") == "true",
        "ai_enabled": form.get("ai_enabled") == "true",
        "ai_require_user_keys": form.get("ai_require_user_keys") == "true",
    }
    # Only update SMTP password if a new value was provided
    if smtp_password_plain:
        data["smtp_password_encrypted"] = encrypt(smtp_password_plain)

    try:
        s = await update_app_settings(db, data)
        await log_audit(db, user.id, "app_settings_update", target_type="app_settings", target_id=1)
        return templates.TemplateResponse(request, "admin/settings.html", {
            "s": s,
            "saved": True,
            "error": None,
        })
    except Exception as e:
        logger.error("Failed to save app settings: %s", e)
        return templates.TemplateResponse(request, "admin/settings.html", {
            "s": s,
            "saved": False,
            "error": "Failed to save settings.",
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
        smtp_port=_safe_int(form.get("smtp_port"), saved.smtp_port or 587),
        smtp_user=form.get("smtp_user", "").strip() or saved.smtp_user,
        smtp_from_email=form.get("smtp_from_email", "").strip() or saved.smtp_from_email,
        smtp_use_tls=form.get("smtp_use_tls") == "true" if "smtp_use_tls" in form else saved.smtp_use_tls,
        # Use new password if provided, otherwise keep saved encrypted password
        smtp_password_encrypted=(
            encrypt(form["smtp_password"].strip())
            if form.get("smtp_password", "").strip()
            else saved.smtp_password_encrypted
        ),
    )

    try:
        send_email(
            s,
            to=user.email,
            subject="Filtread – SMTP test",
            body=f"This is a test email sent from Filtread admin panel to {user.email}.",
        )
        return HTMLResponse(
            '<span class="text-green-600">Test email sent to ' + user.email + ".</span>"
        )
    except ValueError as e:
        logger.warning("SMTP test – not configured: %s", e)
        return HTMLResponse('<span class="text-red-600">SMTP not configured (missing host or from address).</span>')
    except Exception as e:
        logger.error("SMTP test failed: %s", e)
        return HTMLResponse('<span class="text-red-600">Failed to send test email. Check server logs for details.</span>')


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
    revoked = await revoke_invitation(db, invitation_id)
    if revoked:
        await log_audit(db, user.id, "invitation_revoke", target_type="invitation", target_id=invitation_id)
    invitations = await list_invitations(db)
    return templates.TemplateResponse(request, "admin/partials/invitations_table.html", {
        "invitations": invitations,
        "base_url": str(request.base_url).rstrip("/"),
    })


# ── Feeds ─────────────────────────────────────────────────────────────────────

@router.get("/feeds", response_class=HTMLResponse)
async def admin_feeds(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    feeds = await list_feeds_with_stats(db)
    return templates.TemplateResponse(request, "admin/feeds.html", {"feeds": feeds})


# ── Feed actions ──────────────────────────────────────────────────────────────

async def _feeds_response(request: Request, db, user) -> HTMLResponse:
    feeds = await list_feeds_with_stats(db)
    return templates.TemplateResponse(request, "admin/partials/feeds_table.html", {"feeds": feeds})


@router.post("/feeds/{feed_id}/pause", response_class=HTMLResponse)
async def admin_toggle_feed_pause(
    feed_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    feed = await toggle_feed_pause(db, feed_id)
    if feed:
        await log_audit(db, user.id, "feed_pause_toggle", target_type="feed", target_id=feed_id,
                        detail={"status": feed.status})
    return await _feeds_response(request, db, user)


@router.post("/feeds/{feed_id}/clear-error", response_class=HTMLResponse)
async def admin_clear_feed_error(
    feed_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    cleared = await clear_feed_error(db, feed_id)
    if cleared:
        await log_audit(db, user.id, "feed_clear_error", target_type="feed", target_id=feed_id)
    return await _feeds_response(request, db, user)


@router.delete("/feeds/{feed_id}", response_class=HTMLResponse)
async def admin_delete_feed(
    feed_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    deleted = await delete_feed(db, feed_id)
    if deleted:
        await log_audit(db, user.id, "feed_delete", target_type="feed", target_id=feed_id)
    return await _feeds_response(request, db, user)


@router.post("/feeds/{feed_id}/force-fetch", response_class=HTMLResponse)
async def admin_force_fetch(
    feed_id: int,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.fetcher.rss import fetch_feed
    feed = await get_feed(db, feed_id)
    if feed:
        await fetch_feed(feed, db)
        await log_audit(db, user.id, "feed_force_fetch", target_type="feed", target_id=feed_id)
    return await _feeds_response(request, db, user)


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

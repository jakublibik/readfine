"""Web routes for account profile: name, email, password, and account deletion."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.auth.security import (
    generate_token,
    hash_password,
    hash_token,
    password_within_limit,
    verify_password,
)
from app.database import get_db
from app.models.settings import AppSettings
from app.models.user import User
from app.services.feed import cleanup_user_feeds
from app.templating import templates
from app.utils.email_validate import is_valid_email
from app.utils.smtp import send_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["settings"])


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
    if not is_valid_email(email):
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "Please enter a valid email address.",
        })
    if not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "Current password is incorrect.",
        })
    if email == user.email:
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "This is already your email address.",
        })
    existing = await db.execute(
        select(User).where(User.email == email, User.id != user.id)
    )
    if existing.scalar_one_or_none():
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_error": "This email is already in use.",
        })

    app_s = await db.scalar(select(AppSettings).where(AppSettings.id == 1))
    smtp_configured = bool(app_s and app_s.smtp_host)

    if not smtp_configured:
        # No SMTP → reset/briefing emails don't work anyway; change immediately
        # (current_password already protects this endpoint).
        user.email = email
        await db.commit()
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "email_saved": True,
        })

    # Pending-email flow: verify the new address before switching.
    token = generate_token()
    user.pending_email = email
    user.pending_email_token_hash = hash_token(token)
    user.pending_email_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    old_email = user.email
    await db.commit()

    verify_url = str(request.base_url) + f"verify-email-change?token={token}"
    try:
        await asyncio.to_thread(
            send_email, app_s, email,
            "Readfine – Confirm your new email address",
            f"Please confirm your new email address by clicking the link below:\n\n{verify_url}\n\nThis link expires in 24 hours.\n\nIf you did not request this change, you can safely ignore this email.",
        )
    except Exception as e:
        logger.error("Failed to send email-change verification to %s: %s", email, e)
    # Heads-up to the current address so a silent takeover is detectable.
    try:
        await asyncio.to_thread(
            send_email, app_s, old_email,
            "Readfine – Email change requested",
            f"A request was made to change your Readfine email address to {email}.\n\nIf this was not you, please change your password immediately.",
        )
    except Exception as e:
        logger.error("Failed to send email-change notice to %s: %s", old_email, e)

    return templates.TemplateResponse(request, "settings/profile.html", {
        "user": user,
        "email_pending": email,
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
    if not password_within_limit(new_pw):
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "pw_error": "New password is too long (max 72 characters).",
        })
    if new_pw != confirm:
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "pw_error": "Passwords do not match.",
        })

    user.password_hash = hash_password(new_pw)
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    # Invalidate all existing sessions/JWTs, then keep the current session alive.
    user.session_token_version += 1
    await db.commit()
    request.session["tv"] = user.session_token_version
    return templates.TemplateResponse(request, "settings/profile.html", {
        "user": user,
        "pw_saved": True,
    })


@router.post("/profile/delete-account", response_class=HTMLResponse)
async def settings_profile_delete_account(
    request: Request,
    current_password: str = Form(...),
    confirm_text: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.role == "admin":
        return Response(status_code=403)
    if confirm_text.strip().lower() != "delete my account":
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "delete_error": "Please type 'delete my account' exactly to confirm.",
        })
    if not verify_password(current_password, user.password_hash):
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "delete_error": "Password is incorrect.",
        })
    try:
        await cleanup_user_feeds(user.id, db)
        await db.delete(user)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Failed to delete account for user %s", user.id)
        return templates.TemplateResponse(request, "settings/profile.html", {
            "user": user,
            "delete_error": "Could not delete your account due to a server error. Please try again later.",
        }, status_code=500)
    request.session.clear()
    return RedirectResponse("/login?deleted=1", status_code=303)

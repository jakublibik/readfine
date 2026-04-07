import asyncio
import hashlib
import logging
import secrets

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta, timezone

from app.auth.security import hash_password, verify_password
from app.config import settings as app_settings_config
from app.database import get_db
from app.rate_limit import limiter
from app.models.auth import Invitation
from app.models.user import User, UserSettings
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-auth"])
templates = Jinja2Templates(directory="app/templates")


async def _get_app_settings(db: AsyncSession) -> AppSettings | None:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    return result.scalar_one_or_none()


async def _get_valid_invitation(db: AsyncSession, token: str) -> Invitation | None:
    """Return invitation if token is valid (exists, unused, not expired)."""
    result = await db.execute(select(Invitation).where(Invitation.token == token))
    inv = result.scalar_one_or_none()
    if not inv or inv.used_at is not None:
        return None
    if inv.expires_at and inv.expires_at < datetime.now(timezone.utc):
        return None
    return inv


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/app", status_code=302)
    return templates.TemplateResponse(request, "auth/login.html")


@router.post("/login", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_login)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        app_settings = await _get_app_settings(db)
        smtp_configured = bool(app_settings and app_settings.smtp_host)
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Invalid email or password", "email": email, "show_reset": smtp_configured},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.is_active:
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Account is disabled", "email": email},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    user.last_active_at = datetime.now(timezone.utc)
    await db.commit()

    request.session["user_id"] = user.id
    return RedirectResponse("/app", status_code=302)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, invite: str | None = None, db: AsyncSession = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse("/app", status_code=302)
    app_settings = await _get_app_settings(db)
    registration_open = not app_settings or app_settings.registration_enabled

    if invite:
        inv = await _get_valid_invitation(db, invite)
        if not inv:
            return templates.TemplateResponse(
                request, "auth/registration_disabled.html",
                {"error": "This invitation link is invalid or has already been used."},
            )
        return templates.TemplateResponse(request, "auth/register.html", {
            "invite_token": invite,
            "prefill_email": inv.email or "",
            "email_locked": bool(inv.email),
        })

    if not registration_open:
        return templates.TemplateResponse(request, "auth/registration_disabled.html")

    return templates.TemplateResponse(request, "auth/register.html")


@router.post("/register", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_register)
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    invite_token: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await _get_app_settings(db)
    registration_open = not app_settings or app_settings.registration_enabled

    # Validate invite token if provided or required
    inv = None
    if invite_token:
        inv = await _get_valid_invitation(db, invite_token)
        if not inv:
            return templates.TemplateResponse(
                request, "auth/register.html",
                {"error": "This invitation link is invalid or has already been used.", "invite_token": invite_token},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        # If invite is locked to specific email, enforce it
        if inv.email and inv.email.lower() != email.lower():
            return templates.TemplateResponse(
                request, "auth/register.html",
                {"error": "This invitation is for a different email address.",
                 "invite_token": invite_token, "prefill_email": inv.email, "email_locked": True},
                status_code=status.HTTP_400_BAD_REQUEST,
            )
    elif not registration_open:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration disabled")

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "This email is already registered", "invite_token": invite_token},
            status_code=status.HTTP_409_CONFLICT,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "Password must be at least 8 characters", "invite_token": invite_token},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    display_name = display_name.strip()
    if not display_name:
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "Display name cannot be empty", "invite_token": invite_token},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        role="user",
    )
    db.add(user)
    await db.flush()

    db.add(UserSettings(user_id=user.id))

    if inv:
        inv.used_at = datetime.now(timezone.utc)
        inv.used_by = user.id

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "This email is already registered", "invite_token": invite_token},
            status_code=status.HTTP_409_CONFLICT,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/app", status_code=302)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Password reset ─────────────────────────────────────────────────────────────

@router.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(request: Request):
    return templates.TemplateResponse(request, "auth/reset_password_request.html")


@router.post("/reset-password", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_reset_password)
async def reset_password_request(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # Always show success to avoid email enumeration
    app_settings = await _get_app_settings(db)
    if not app_settings or not app_settings.smtp_host:
        return templates.TemplateResponse(request, "auth/reset_password_request.html",
                                          {"sent": True})

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user and user.is_active:
        token = secrets.token_urlsafe(32)
        user.password_reset_token = hashlib.sha256(token.encode()).hexdigest()
        user.password_reset_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        await db.commit()

        reset_url = str(request.base_url) + f"reset-password/{token}"
        try:
            from app.utils.smtp import send_email
            await asyncio.to_thread(
                send_email,
                app_settings,
                user.email,
                "Readfine – Password reset",
                f"Click the link below to reset your password (valid for 1 hour):\n\n{reset_url}\n\nIf you did not request this, ignore this email.",
            )
        except Exception as e:
            logger.error("Failed to send password reset email to %s: %s", email, e)

    return templates.TemplateResponse(request, "auth/reset_password_request.html", {"sent": True})


@router.get("/reset-password/{token}", response_class=HTMLResponse)
async def reset_password_confirm_page(
    token: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_by_reset_token(db, token)
    if not user:
        return templates.TemplateResponse(request, "auth/reset_password_confirm.html",
                                          {"invalid": True})
    return templates.TemplateResponse(request, "auth/reset_password_confirm.html",
                                      {"token": token})


@router.post("/reset-password/{token}", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_reset_password)
async def reset_password_confirm(
    token: str,
    request: Request,
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await _get_user_by_reset_token(db, token)
    if not user:
        return templates.TemplateResponse(request, "auth/reset_password_confirm.html",
                                          {"invalid": True})
    if len(new_password) < 8:
        return templates.TemplateResponse(request, "auth/reset_password_confirm.html",
                                          {"token": token, "error": "Password must be at least 8 characters."})
    if new_password != confirm_password:
        return templates.TemplateResponse(request, "auth/reset_password_confirm.html",
                                          {"token": token, "error": "Passwords do not match."})

    user.password_hash = hash_password(new_password)
    user.password_reset_token = None
    user.password_reset_expires_at = None
    await db.commit()
    return templates.TemplateResponse(request, "auth/reset_password_confirm.html", {"done": True})


async def _get_user_by_reset_token(db: AsyncSession, token: str) -> User | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(select(User).where(User.password_reset_token == token_hash))
    user = result.scalar_one_or_none()
    if not user or not user.password_reset_expires_at:
        return None
    if user.password_reset_expires_at < datetime.now(timezone.utc):
        return None
    return user

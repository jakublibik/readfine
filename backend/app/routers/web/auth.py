import asyncio
import hashlib
import logging
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime, timedelta, timezone

from app.auth.security import hash_password, verify_password
from app.utils.smtp import send_email
from app.utils.datetime_format import is_valid_timezone
from app.config import settings as app_settings_config
from app.database import get_db
from app.rate_limit import limiter, check_login_lockout, record_failed_login, clear_failed_logins, get_client_ip
from app.models.auth import Invitation
from app.models.user import User, UserSettings
from app.models.settings import AppSettings

logger = logging.getLogger(__name__)

from app.templating import templates

router = APIRouter(tags=["web-auth"])


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


@router.get("/")
async def root(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/app", status_code=302)
    return RedirectResponse(url="/login", status_code=302)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse("/app", status_code=302)
    app_settings = await _get_app_settings(db)
    registration_open = not app_settings or app_settings.registration_enabled
    return templates.TemplateResponse(request, "auth/login.html", {"registration_open": registration_open})


@router.post("/login", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_login)
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await _get_app_settings(db)
    smtp_configured = bool(app_settings and app_settings.smtp_host)
    registration_open = not app_settings or app_settings.registration_enabled

    ip = get_client_ip(request)

    def _login_err(msg: str, http_status: int, **extra):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": msg, "email": email, "registration_open": registration_open, **extra},
            status_code=http_status,
        )

    if check_login_lockout(ip, email):
        return _login_err("Too many failed attempts. Try again in 15 minutes.", status.HTTP_429_TOO_MANY_REQUESTS)

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        record_failed_login(ip, email)
        return _login_err("Invalid email or password", status.HTTP_401_UNAUTHORIZED,
                          show_reset=smtp_configured)

    if not user.is_active:
        return _login_err("Account is disabled", status.HTTP_403_FORBIDDEN)

    if not user.email_verified:
        return _login_err("Email not verified.", status.HTTP_403_FORBIDDEN, show_resend=True)

    clear_failed_logins(ip, email)
    user.last_active_at = datetime.now(timezone.utc)
    await db.commit()

    request.session["user_id"] = user.id
    request.session["tv"] = user.session_token_version
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
    confirm_password: str = Form(...),
    display_name: str = Form(""),
    invite_token: str = Form(""),
    tz: str = Form("", alias="timezone"),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await _get_app_settings(db)
    registration_open = not app_settings or app_settings.registration_enabled

    def _err(msg: str, http_status: int = status.HTTP_422_UNPROCESSABLE_ENTITY, **extra):
        ctx = {"error": msg, "invite_token": invite_token,
               "prefill_email": email, "prefill_display_name": display_name, **extra}
        return templates.TemplateResponse(request, "auth/register.html", ctx, status_code=http_status)

    # Validate invite token if provided or required
    inv = None
    if invite_token:
        inv = await _get_valid_invitation(db, invite_token)
        if not inv:
            return _err("This invitation link is invalid or has already been used.",
                        http_status=status.HTTP_400_BAD_REQUEST)
        # If invite is locked to specific email, enforce it
        if inv.email and inv.email.lower() != email.lower():
            return _err("This invitation is for a different email address.",
                        http_status=status.HTTP_400_BAD_REQUEST,
                        prefill_email=inv.email, email_locked=True)
    elif not registration_open:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration disabled")

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return _err("This email is already registered.", http_status=status.HTTP_409_CONFLICT)

    if len(password) < 8:
        return _err("Password must be at least 8 characters.")

    if password != confirm_password:
        return _err("Passwords do not match.")

    display_name = display_name.strip() or email.split("@")[0]

    smtp_configured = bool(app_settings and app_settings.smtp_host)
    # Invite with locked email = admin-verified address; no SMTP = skip verification
    needs_verification = smtp_configured and not (inv and inv.email)

    token = None
    if needs_verification:
        token = secrets.token_urlsafe(32)

    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        role="user",
        email_verified=not needs_verification,
        email_verification_token_hash=hashlib.sha256(token.encode()).hexdigest() if token else None,
        email_verification_expires_at=datetime.now(timezone.utc) + timedelta(hours=24) if token else None,
    )
    db.add(user)
    await db.flush()

    user_tz = tz.strip() if is_valid_timezone(tz.strip()) else "UTC"
    db.add(UserSettings(user_id=user.id, timezone=user_tz))

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

    if needs_verification:
        verify_url = str(request.base_url) + f"verify-email?token={token}"
        try:
            await asyncio.to_thread(
                send_email,
                app_settings,
                email,
                "Readfine – Verify your email address",
                f"Please verify your email address by clicking the link below:\n\n{verify_url}\n\nThis link expires in 24 hours.\n\nIf you did not create a Readfine account, you can safely ignore this email.",
            )
        except Exception as e:
            logger.error("Failed to send verification email to %s: %s", email, e)
        return RedirectResponse(f"/register/check-email?email={quote(email, safe='')}&sent=1", status_code=302)

    request.session["user_id"] = user.id
    request.session["tv"] = user.session_token_version
    return RedirectResponse("/app", status_code=302)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)


# ── Email verification ─────────────────────────────────────────────────────────

@router.get("/register/check-email", response_class=HTMLResponse)
async def check_email_page(request: Request, email: str = ""):
    return templates.TemplateResponse(request, "auth/check_email.html", {"email": email})


@router.get("/verify-email", response_class=HTMLResponse)
async def verify_email(request: Request, token: str = "", db: AsyncSession = Depends(get_db)):
    if not token:
        return templates.TemplateResponse(request, "auth/check_email.html",
                                          {"verify_error": True, "email": ""})
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(select(User).where(User.email_verification_token_hash == token_hash))
    user = result.scalar_one_or_none()
    if not user or not user.email_verification_expires_at:
        return templates.TemplateResponse(request, "auth/check_email.html",
                                          {"verify_error": True, "email": ""})
    if user.email_verification_expires_at < datetime.now(timezone.utc):
        return templates.TemplateResponse(request, "auth/check_email.html",
                                          {"verify_error": True, "email": user.email})
    if not user.is_active:
        return templates.TemplateResponse(request, "auth/check_email.html",
                                          {"verify_error": True, "email": ""})
    user.email_verified = True
    user.email_verification_token_hash = None
    user.email_verification_expires_at = None
    await db.commit()
    return RedirectResponse("/login?verified=1", status_code=302)


@router.post("/resend-verification", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_reset_password)
async def resend_verification(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await _get_app_settings(db)
    if app_settings and app_settings.smtp_host:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()
        if user and not user.email_verified:
            token = secrets.token_urlsafe(32)
            user.email_verification_token_hash = hashlib.sha256(token.encode()).hexdigest()
            user.email_verification_expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
            await db.commit()
            verify_url = str(request.base_url) + f"verify-email?token={token}"
            try:
                await asyncio.to_thread(
                    send_email,
                    app_settings,
                    email,
                    "Readfine – Verify your email address",
                    f"Please verify your email address by clicking the link below:\n\n{verify_url}\n\nThis link expires in 24 hours.\n\nIf you did not create a Readfine account, you can safely ignore this email.",
                )
            except Exception as e:
                logger.error("Failed to resend verification email to %s: %s", email, e)
    return RedirectResponse(f"/register/check-email?email={quote(email, safe='')}&resent=1", status_code=302)


@router.get("/verify-email-change", response_class=HTMLResponse)
async def verify_email_change(request: Request, token: str = "", db: AsyncSession = Depends(get_db)):
    def _result(ok: bool, message: str):
        return templates.TemplateResponse(
            request, "auth/email_change_result.html",
            {"ok": ok, "message": message},
            status_code=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST,
        )

    if not token:
        return _result(False, "This confirmation link is invalid.")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(select(User).where(User.pending_email_token_hash == token_hash))
    user = result.scalar_one_or_none()
    if not user or not user.pending_email or not user.pending_email_expires_at:
        return _result(False, "This confirmation link is invalid.")
    if user.pending_email_expires_at < datetime.now(timezone.utc):
        return _result(False, "This confirmation link has expired. Please request the change again.")

    # Re-check the target address is still free (race with another signup/change).
    taken = await db.execute(
        select(User).where(User.email == user.pending_email, User.id != user.id)
    )
    if taken.scalar_one_or_none():
        user.pending_email = None
        user.pending_email_token_hash = None
        user.pending_email_expires_at = None
        await db.commit()
        return _result(False, "That email address is no longer available. Please choose another.")

    user.email = user.pending_email
    user.pending_email = None
    user.pending_email_token_hash = None
    user.pending_email_expires_at = None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return _result(False, "That email address is no longer available. Please choose another.")
    return _result(True, "Your email address has been updated.")


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
        user.password_reset_token_hash = hashlib.sha256(token.encode()).hexdigest()
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
    user.password_reset_token_hash = None
    user.password_reset_expires_at = None
    # Invalidate all existing sessions/JWTs; user logs in again with new password.
    user.session_token_version += 1
    await db.commit()
    return templates.TemplateResponse(request, "auth/reset_password_confirm.html", {"done": True})


async def _get_user_by_reset_token(db: AsyncSession, token: str) -> User | None:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(select(User).where(User.password_reset_token_hash == token_hash))
    user = result.scalar_one_or_none()
    if not user or not user.password_reset_expires_at:
        return None
    if user.password_reset_expires_at < datetime.now(timezone.utc):
        return None
    return user

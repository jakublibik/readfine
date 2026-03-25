from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import hash_password, verify_password
from app.database import get_db
from app.main import limiter
from app.models.user import User, UserSettings
from app.models.settings import AppSettings

router = APIRouter(tags=["web-auth"])
templates = Jinja2Templates(directory="app/templates")


async def _get_app_settings(db: AsyncSession) -> AppSettings | None:
    result = await db.execute(select(AppSettings).where(AppSettings.id == 1))
    return result.scalar_one_or_none()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse("/app", status_code=302)
    return templates.TemplateResponse(request, "auth/login.html")


@router.post("/login", response_class=HTMLResponse)
@limiter.limit("10/minute")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Invalid email or password", "email": email},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    if not user.is_active:
        return templates.TemplateResponse(
            request, "auth/login.html",
            {"error": "Account is disabled", "email": email},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/app", status_code=302)


@router.get("/register", response_class=HTMLResponse)
async def register_page(request: Request, db: AsyncSession = Depends(get_db)):
    if request.session.get("user_id"):
        return RedirectResponse("/app", status_code=302)
    app_settings = await _get_app_settings(db)
    if app_settings and not app_settings.registration_enabled:
        return templates.TemplateResponse(request, "auth/registration_disabled.html")
    return templates.TemplateResponse(request, "auth/register.html")


@router.post("/register", response_class=HTMLResponse)
@limiter.limit("5/minute")
async def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await _get_app_settings(db)
    if app_settings and not app_settings.registration_enabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Registration disabled")

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "This email is already registered"},
            status_code=status.HTTP_409_CONFLICT,
        )

    if len(password) < 8:
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "Password must be at least 8 characters"},
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    display_name = display_name.strip()
    if not display_name:
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "Display name cannot be empty"},
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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return templates.TemplateResponse(
            request, "auth/register.html",
            {"error": "This email is already registered"},
            status_code=status.HTTP_409_CONFLICT,
        )

    request.session["user_id"] = user.id
    return RedirectResponse("/app", status_code=302)


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

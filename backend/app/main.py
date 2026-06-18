import asyncio
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler
from fastapi.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette_csrf import CSRFMiddleware
from slowapi.errors import RateLimitExceeded

from sqlalchemy import select

from app.config import settings
from app.rate_limit import limiter
import app.database as db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=20))
    db.engine = db.create_engine(settings.database_url)
    db.async_session_factory = db.create_session_factory(db.engine)

    if settings.first_admin_email and settings.first_admin_password:
        from app.services.user import seed_first_admin
        async with db.async_session_factory() as session:
            await seed_first_admin(session, settings.first_admin_email, settings.first_admin_password)

    from app.models.settings import AppSettings
    from app.templating import set_ai_enabled
    async with db.async_session_factory() as session:
        row = await session.scalar(select(AppSettings).where(AppSettings.id == 1))
        if row:
            set_ai_enabled(row.ai_enabled)

    from app.fetcher.scheduler import create_scheduler
    sched = create_scheduler()
    sched.start()

    yield

    # Shutdown
    sched.shutdown(wait=True)
    await db.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
    )

    # Middleware (order matters – outermost first)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=settings.allowed_hosts,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=not settings.debug,
        same_site="lax",
        max_age=14 * 24 * 3600,  # 14-day sliding expiry; re-stamped on each response
    )
    app.add_middleware(
        CSRFMiddleware,
        secret=settings.secret_key,
        # API uses Bearer tokens; auth forms are exempt (no session yet, or low-risk logout)
        exempt_urls=[
            re.compile(r"^/api/"),
            re.compile(r"^/login$"),
            re.compile(r"^/logout$"),
            re.compile(r"^/register$"),
            re.compile(r"^/reset-password"),
            re.compile(r"^/resend-verification$"),
        ],
        sensitive_cookies={"session"},
    )

    # Rate limiting
    from app.templating import templates as _templates

    async def _html_rate_limit_handler(request: Request, exc: RateLimitExceeded) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        return _templates.TemplateResponse(
            request, "errors/429.html", {}, status_code=429
        )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _html_rate_limit_handler)

    # Security headers
    @app.middleware("http")
    async def security_headers(request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not settings.debug:
            # 'unsafe-eval' is intentionally retained: HTMX evaluates several
            # template-authored expressions via the Function constructor —
            # hx-on::*, hx-vals="js:…", and hx-trigger event filters (e.g.
            # click[…], keydown[key=='Enter']). All are developer-authored, not
            # user input, so this is not an active injection vector; the primary
            # XSS defense is the nonce on script-src (injected inline scripts
            # can't run). Removing it requires migrating those usages to external
            # JS first — tracked as a post-launch hardening task (review M3).
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'unsafe-eval' 'nonce-{nonce}'; "
                "img-src * data:; "
                "style-src 'self' 'unsafe-inline'; "
                "connect-src 'self';"
            )
        return response

    # Static files
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # Routers
    from app.routers.web.auth import router as web_auth_router
    from app.routers.web.app import router as web_app_router
    from app.routers.web.settings import router as web_settings_router
    from app.routers.web.admin import router as web_admin_router
    from app.routers.web.legal import router as web_legal_router
    from app.routers.api.v1.auth import router as api_auth_router
    from app.routers.api.v1.folders import router as api_folders_router
    from app.routers.api.v1.feeds import router as api_feeds_router
    from app.routers.api.v1.articles import router as api_articles_router
    from app.routers.api.v1.labels import router as api_labels_router
    from app.routers.api.v1.filters import router as api_filters_router

    app.include_router(web_auth_router)
    app.include_router(web_app_router)
    app.include_router(web_settings_router)
    app.include_router(web_admin_router)
    app.include_router(web_legal_router)
    app.include_router(api_auth_router, prefix="/api/v1")
    app.include_router(api_folders_router, prefix="/api/v1")
    app.include_router(api_feeds_router, prefix="/api/v1")
    app.include_router(api_articles_router, prefix="/api/v1")
    app.include_router(api_labels_router, prefix="/api/v1")
    app.include_router(api_filters_router, prefix="/api/v1")

    from starlette.exceptions import HTTPException as _StarletteHTTPException

    async def auth_redirect_handler(request: Request, exc: _StarletteHTTPException):
        is_api = request.url.path.startswith("/api/")
        if exc.status_code == 401 and not is_api:
            if request.headers.get("HX-Request"):
                return Response(status_code=200, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=302)
        if exc.status_code == 404 and not is_api:
            return _templates.TemplateResponse(request, "errors/404.html", {}, status_code=404)
        return await _default_http_exception_handler(request, exc)

    app.add_exception_handler(_StarletteHTTPException, auth_redirect_handler)

    @app.exception_handler(Exception)
    async def server_error_handler(request: Request, exc: Exception):
        import logging
        logging.getLogger(__name__).exception("Unhandled exception: %s", exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return _templates.TemplateResponse(request, "errors/500.html", {}, status_code=500)

    return app


app = create_app()

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler
from fastapi.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette_csrf import CSRFMiddleware
from slowapi import _rate_limit_exceeded_handler
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
        ],
        sensitive_cookies={"session"},
    )

    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Security headers
    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not settings.debug:
            # Note: <script type="application/json"> is a data block, not executable —
            # it is NOT covered by script-src and does not require unsafe-inline.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-eval' 'sha256-0uxrvilPUJCT3k/+dqd7J1+BNEI+pjD5pHGdBigJAS0=' 'sha256-Ndjk6JNMLJ7YWddVtAwiNzMrOWCG3u03r3HXuWjNL/0=' 'sha256-yF/AgXJr5eU+0PI4tdElAq5mc3MPZWQhf3eCtLYeOYA=' 'sha256-fEeZZqxjvf8KnXGpUawHozmwW1PaiGqeIEY/BIC5WBE=' 'sha256-Q8Zlqz4i97vmfKzpNOgDaGfkYpIKtKxHrdRVMP9IYxg=' 'sha256-Qg3X8MilaCm0DyUmYgpGr6Ak7XcyNG4P7fYBbx1w4HE='; "  # unsafe-eval: htmx.js; hashes: main.html, share.html, preferences.html, scrape_setup.html, catch_me_up.html (×2); scope_selector.html → external JS
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
    app.include_router(api_auth_router, prefix="/api/v1")
    app.include_router(api_folders_router, prefix="/api/v1")
    app.include_router(api_feeds_router, prefix="/api/v1")
    app.include_router(api_articles_router, prefix="/api/v1")
    app.include_router(api_labels_router, prefix="/api/v1")
    app.include_router(api_filters_router, prefix="/api/v1")

    @app.exception_handler(HTTPException)
    async def auth_redirect_handler(request: Request, exc: HTTPException):
        if exc.status_code == 401 and not request.url.path.startswith("/api/"):
            if request.headers.get("HX-Request"):
                return Response(status_code=200, headers={"HX-Redirect": "/login"})
            return RedirectResponse("/login", status_code=302)
        return await _default_http_exception_handler(request, exc)

    return app


app = create_app()

import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette_csrf import CSRFMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import settings
from app.rate_limit import limiter
import app.database as db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db.engine = db.create_engine(settings.database_url)
    db.async_session_factory = db.create_session_factory(db.engine)

    if settings.first_admin_email and settings.first_admin_password:
        from app.services.user import seed_first_admin
        async with db.async_session_factory() as session:
            await seed_first_admin(session, settings.first_admin_email, settings.first_admin_password)

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
        exempt_urls=[re.compile(r"^/api/")],  # API uses Bearer tokens
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
                "script-src 'self' https://cdn.tailwindcss.com https://unpkg.com; "
                "img-src * data:; "
                "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
                "connect-src 'self' https://cdn.tailwindcss.com;"
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

    return app


app = create_app()

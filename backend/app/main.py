from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.sessions import SessionMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings
import app.database as db


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    db.engine = db.create_engine(settings.database_url)
    db.async_session_factory = db.create_session_factory(db.engine)

    yield

    # Shutdown
    await db.engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        # Disable OpenAPI in production
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
    )

    # Middleware
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

    # Rate limiting
    limiter = Limiter(key_func=get_remote_address)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Static files
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # Security headers middleware
    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if not settings.debug:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "img-src * data:; "
                "style-src 'self' 'unsafe-inline';"
            )
        return response

    return app


app = create_app()

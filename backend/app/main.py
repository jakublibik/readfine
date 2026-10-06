import asyncio
import json
import re
import secrets
from urllib.parse import quote
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

from sqlalchemy import select, text

from app.config import settings
from app.logging_config import configure_logging
from app.rate_limit import limiter
import app.database as db

# Before anything else logs: uvicorn leaves the root logger unconfigured.
configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    if settings.debug:
        import logging
        logging.getLogger(__name__).warning(
            "DEBUG mode is ON — development profile active: session cookies are "
            "NOT marked Secure, /docs is exposed, and the insecure-config guard "
            "is bypassed. Never run with DEBUG=true in production."
        )
    elif not settings.session_cookie_is_secure:
        import logging
        logging.getLogger(__name__).warning(
            "SESSION_COOKIE_SECURE=false: the session cookie also travels over plain "
            "HTTP, where anyone on the network can read it. Fine for a home LAN, "
            "not for an instance reachable from the internet."
        )
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=20))
    db.engine = db.create_engine(settings.database_url)
    db.async_session_factory = db.create_session_factory(db.engine)

    if settings.first_admin_email and settings.first_admin_password:
        from app.services.user import seed_first_admin
        async with db.async_session_factory() as session:
            await seed_first_admin(session, settings.first_admin_email, settings.first_admin_password)

    from app.models.settings import AppSettings
    from app.services import traffic_service
    from app.templating import set_ai_enabled, set_feedback_available
    async with db.async_session_factory() as session:
        row = await session.scalar(select(AppSettings).where(AppSettings.id == 1))
        if row:
            set_ai_enabled(row.ai_enabled)
            set_feedback_available(
                bool(row.feedback_enabled and row.smtp_host and row.smtp_from_email)
            )
            traffic_service.set_enabled(row.traffic_stats_enabled)

    # Hydrate the learned per-host fetch spacing so it survives restarts/deploys.
    from app.services.host_rate_limit_service import load_into_memory
    async with db.async_session_factory() as session:
        await load_into_memory(session)

    # Backfill/refresh adaptive fetch intervals now (writes only changed feeds), then
    # the daily job keeps them current.
    from app.fetcher.scheduler import recompute_derived_intervals
    async with db.async_session_factory() as session:
        await recompute_derived_intervals(session)

    from app.fetcher.scheduler import create_scheduler
    sched = create_scheduler()
    sched.start()

    yield

    # Shutdown
    sched.shutdown(wait=True)
    # A subscribe or a save just before the deploy is still fetching in the background.
    from app.utils.background import wait_background
    await wait_background(timeout=5)
    from app.services.host_rate_limit_service import flush
    async with db.async_session_factory() as session:
        await flush(session)
    # The last minute of traffic counts, which the flush job would otherwise lose to
    # the deploy.
    if traffic_service.get_enabled():
        async with db.async_session_factory() as session:
            await traffic_service.flush(session)
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
        https_only=settings.session_cookie_is_secure,
        same_site="lax",
        # Sliding expiry, re-stamped on each response; see session_max_age_days.
        max_age=settings.session_max_age_days * 24 * 3600,
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
        cookie_secure=settings.session_cookie_is_secure,
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
    from app.services.traffic_service import record as record_visit

    @app.middleware("http")
    async def security_headers(request, call_next):
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        # Matches the <meta name="referrer"> in base.html, which is there so the policy
        # is visible where the pages that depend on it are written. Article images are
        # hotlinked from the publisher and some hosts refuse a foreign referrer
        # (maps.wikimedia.org answers 403, which cost every MediaWiki locator map), so
        # nothing is sent cross-origin. Same-origin requests keep a full referrer.
        # Both are set to the same value deliberately: a document takes the meta over
        # the header, since it is parsed later, and there is no reason to make anyone
        # work that out from two places that disagree.
        response.headers["Referrer-Policy"] = "same-origin"
        # CSP is sent in every environment (it does not depend on HTTPS and the
        # templates render identically in dev and prod), so XSS protection is
        # never silently dropped by DEBUG. No 'unsafe-eval': htmx runs with
        # allowEval off (base.html), so hx-on, hx-vals="js:" and hx-trigger filters
        # are not used, and HTML that reaches the page through a swap cannot run code
        # through them either. Inline scripts need the nonce, which htmx no longer
        # copies onto scripts in swapped content.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "img-src * data:; "
            "style-src 'self' 'unsafe-inline'; "
            # The two video players an article body can hold, and nothing else that
            # frames. Both are only ever loaded after a click on the thumbnail, and
            # both are framed, not scripted: no player API script is loaded, so
            # script-src stays as tight as it was.
            "frame-src https://www.youtube-nocookie.com https://player.vimeo.com; "
            "connect-src 'self';"
        )
        # Authenticated HTML (full pages and HTMX partials) must never be cached: on
        # a shared browser, back/forward or bfcache could otherwise show a previous
        # user's rendered page after an account switch (CWE-525). no-store also
        # disables bfcache in Chrome/Firefox. Static assets are served under /static
        # with non-text/html content types, so they stay cacheable.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie"
        # Public-page visit counting (off unless an admin turned it on). It rides
        # along in this middleware rather than adding one of its own: a second
        # BaseHTTPMiddleware wrapper costs something on every request including
        # /static, and anything added here would sit outside SessionMiddleware,
        # where request.session raises. record() never throws.
        record_visit(request, response)
        return response

    # Health check — dedicated endpoint for uptime/monitoring probes. Unauthenticated
    # and minimal on purpose (no version/internals leaked); does a lightweight DB ping
    # so a probe can distinguish "process up" from "database reachable".
    # GET + HEAD: many uptime monitors (e.g. UptimeRobot) default to HEAD, and FastAPI
    # routes — unlike plain Starlette routes — don't auto-add HEAD for GET (would 405).
    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> JSONResponse:
        try:
            async with db.async_session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "degraded"}, status_code=503)
        return JSONResponse({"status": "ok"})

    # Static files
    app.mount("/static", StaticFiles(directory="app/static"), name="static")

    # Routers
    from app.routers.web.auth import router as web_auth_router
    from app.routers.web.app import router as web_app_router
    from app.routers.web.settings import router as web_settings_router
    from app.routers.web.admin import router as web_admin_router
    from app.routers.web.legal import router as web_legal_router
    from app.routers.web.help import router as web_help_router
    from app.routers.web.pwa import router as web_pwa_router
    from app.routers.api.v1.auth import router as api_auth_router
    from app.routers.api.v1.folders import router as api_folders_router
    from app.routers.api.v1.feeds import router as api_feeds_router
    from app.routers.api.v1.articles import router as api_articles_router
    from app.routers.api.v1.labels import router as api_labels_router
    from app.routers.api.v1.filters import router as api_filters_router
    from app.routers.api.v1.saved_searches import router as api_saved_searches_router

    app.include_router(web_auth_router)
    app.include_router(web_app_router)
    app.include_router(web_settings_router)
    app.include_router(web_admin_router)
    app.include_router(web_legal_router)
    app.include_router(web_help_router)
    app.include_router(web_pwa_router)
    app.include_router(api_auth_router, prefix="/api/v1")
    app.include_router(api_folders_router, prefix="/api/v1")
    app.include_router(api_feeds_router, prefix="/api/v1")
    app.include_router(api_articles_router, prefix="/api/v1")
    app.include_router(api_labels_router, prefix="/api/v1")
    app.include_router(api_filters_router, prefix="/api/v1")
    app.include_router(api_saved_searches_router, prefix="/api/v1")

    from starlette.exceptions import HTTPException as _StarletteHTTPException

    async def auth_redirect_handler(request: Request, exc: _StarletteHTTPException):
        is_api = request.url.path.startswith("/api/")
        if exc.status_code == 401 and not is_api:
            if request.headers.get("HX-Request"):
                return Response(status_code=200, headers={"HX-Redirect": "/login"})
            # A page someone opened from a link (an email, a bookmark) is where
            # they come back to after signing in. An HTMX partial or a form post
            # is not a page to land on, and /app is where login goes anyway.
            target = request.url.path
            if request.method == "GET" and target != "/app":
                if request.url.query:
                    target += "?" + request.url.query
                return RedirectResponse(f"/login?next={quote(target, safe='/')}", status_code=302)
            return RedirectResponse("/login", status_code=302)
        if exc.status_code == 404 and not is_api:
            return _templates.TemplateResponse(request, "errors/404.html", {}, status_code=404)
        return await _default_http_exception_handler(request, exc)

    app.add_exception_handler(_StarletteHTTPException, auth_redirect_handler)

    # Safety nets for input the routes did not check themselves. A web form built into
    # a schema by hand (LabelCreate(...)) raises pydantic's ValidationError, and a name
    # that collides with a unique index raises IntegrityError on flush. Both are the
    # user's input, not a server fault, so they answer 400/409 instead of the 500 page.
    # Routes that check on their own still give the better message; this only catches
    # what slips past. FastAPI's own request validation is a different class
    # (RequestValidationError) and keeps its 422.
    import logging
    from pydantic import ValidationError as _PydanticValidationError
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    _UNIQUE_VIOLATION = "23505"

    def _client_error_response(request: Request, status: int, message: str) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": message}, status_code=status)
        if request.headers.get("HX-Request"):
            # htmx does not swap an error response, but it does fire HX-Trigger, so the
            # message lands in a toast. app.js skips its generic fallback toast for
            # responses that carry one of their own.
            return Response(
                status_code=status,
                headers={"HX-Trigger": json.dumps({"showToast": {"msg": message, "type": "error"}})},
            )
        return _templates.TemplateResponse(
            request, "errors/client.html", {"status": status, "message": message},
            status_code=status,
        )

    async def validation_error_handler(request: Request, exc: _PydanticValidationError):
        # Logged with the trace: the same class also comes from a bug that builds a
        # model from bad data of our own, and that should not pass as a user mistake.
        logging.getLogger(__name__).warning(
            "Validation error on %s %s", request.method, request.url.path, exc_info=exc)
        errors = exc.errors(include_url=False)
        message = "Invalid input."
        if errors:
            field = ".".join(str(p) for p in errors[0].get("loc", ()))
            text_ = errors[0].get("msg", "")
            message = f"Invalid {field}: {text_}." if field else f"Invalid input: {text_}."
        return _client_error_response(request, 400, message)

    async def integrity_error_handler(request: Request, exc: _IntegrityError):
        if db.sqlstate(exc) != _UNIQUE_VIOLATION:
            # A foreign key or NOT NULL failing is our bug, not a duplicate name.
            return await server_error_handler(request, exc)
        logging.getLogger(__name__).warning(
            "Unique violation on %s %s: %s", request.method, request.url.path, exc.orig)
        return _client_error_response(request, 409, "That already exists.")

    app.add_exception_handler(_PydanticValidationError, validation_error_handler)
    app.add_exception_handler(_IntegrityError, integrity_error_handler)

    @app.exception_handler(Exception)
    async def server_error_handler(request: Request, exc: Exception):
        import logging
        logging.getLogger(__name__).exception("Unhandled exception: %s", exc)
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Internal server error"}, status_code=500)
        return _templates.TemplateResponse(request, "errors/500.html", {}, status_code=500)

    return app


app = create_app()

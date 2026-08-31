"""PWA plumbing: the web app manifest and the service worker, served from the site root.

Root, not /static, because a manifest's `scope` and (later) `share_target.action` are
resolved relative to the manifest's own URL, so serving it from a subdirectory invites
paths that quietly mean something else than they read. The worker has a harder
requirement: a worker's scope cannot reach above the directory it is served from, so one
fetched from /static/js/ could only ever control /static/js/.

Unauthenticated on purpose: the manifest is fetched outside normal navigation, and a
manifest behind the session cookie would need `<link rel="manifest"
crossorigin="use-credentials">` to be fetched with credentials at all. It carries no
user data, so there is nothing to protect.
"""
import re
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.templating import templates

router = APIRouter(tags=["pwa"])

_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"

# Not cached at the edge. Cloudflare sits in front of the hosted instance, and a stale
# manifest is how an install ends up pointing at icons or a start_url that no longer
# exist. The file is a few hundred bytes, so revalidating costs nothing.
_NO_CACHE = {"Cache-Control": "no-cache"}


@router.get("/manifest.webmanifest", include_in_schema=False)
async def web_app_manifest() -> FileResponse:
    return FileResponse(
        _STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers=_NO_CACHE,
    )


@router.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    """The service worker, at a fixed URL.

    Registered as plain "/sw.js", never through static_url(): a worker is identified by
    the URL it was registered with, so a cache-busting ?v= would register a new worker
    on every deploy that touched the file and leave the previous one running beside it.
    Updates are the browser's job, and it byte-compares the script itself.
    """
    return FileResponse(
        _STATIC_DIR / "js" / "sw.js",
        media_type="text/javascript",
        headers=_NO_CACHE,
    )


# ── Share target ──────────────────────────────────────────────────────────────
# Android hands a share to whichever field the sending app chose. Plenty of them put
# everything in `text` ("Some headline https://example.com/story") and leave `url`
# empty, so the address has to be dug out rather than read off.
_URL_IN_TEXT = re.compile(r"https?://\S+", re.IGNORECASE)

# A URL at the end of a sentence collects the sentence's punctuation.
_TRAILING_PUNCTUATION = ".,;:!?\"'"
# Closing brackets are different: trimmed only when nothing in the address opened them,
# because plenty of real addresses (Wikipedia disambiguations, most obviously) end in
# one and cutting it silently fetches the wrong page.
_CLOSERS = {")": "(", "]": "[", "}": "{"}

# Enough of a shared title to recognise what is being saved. It is text from another
# app, so there is no length worth trusting.
_TITLE_MAX = 300


def _trim_trailing(candidate: str) -> str:
    while candidate:
        last = candidate[-1]
        if last in _TRAILING_PUNCTUATION:
            candidate = candidate[:-1]
        elif last in _CLOSERS and candidate.count(_CLOSERS[last]) < candidate.count(last):
            candidate = candidate[:-1]
        else:
            break
    return candidate


def _first_url(value: str | None) -> str | None:
    if not value:
        return None
    match = _URL_IN_TEXT.search(value)
    return _trim_trailing(match.group(0)) if match else None


def extract_shared_url(url: str | None, text: str | None) -> str | None:
    """The address a share was about, or None if there is nothing usable in it.

    `url` is preferred because an app that fills it in meant it. Falling through to
    `text` also covers the case where `url` holds something that is not a web address
    at all (a `content://` URI, a bare app name): only http and https ever match, so a
    scheme we cannot fetch is the same as no answer, and the text still gets its turn.

    Where several addresses appear, the first wins. It is a guess either way, which is
    why the page shows what was found in an editable field instead of saving outright.
    """
    for candidate in (url, text):
        found = _first_url(candidate)
        if found:
            return found
    return None


def shared_url_is_certain(url: str | None, text: str | None) -> bool:
    """True when the address was read rather than guessed.

    An app that filled the url field said which address it meant, and one line of text
    holding a single address leaves nothing to choose between. Two of them do: the page
    then has to ask rather than save the wrong one, because saving happens without a
    press whenever this is true.
    """
    if _first_url(url):
        return True
    return len(_URL_IN_TEXT.findall(text or "")) == 1


def _may_save_without_a_press(request: Request) -> bool:
    """Whether this looks like a share rather than a link somebody clicked.

    Saving on load is what makes sharing one step instead of two, but it also means a
    GET sets off a write. The write itself is still a POST from the page, so it keeps
    CSRF and the rate limit; what is left is that a crafted link could fire it for
    anyone signed in. Chrome tells the two apart: a click from another site arrives as
    cross-site, an app hand-off does not.

    Unknown counts as unsafe, so a browser that sends no such header simply gets the
    button. Every refusal here degrades to today's behaviour rather than to an error.
    """
    site = request.headers.get("sec-fetch-site")
    return site is not None and site != "cross-site"


@router.get("/share-target", response_class=HTMLResponse)
async def share_target_form(
    request: Request,
    title: str | None = None,
    text: str | None = None,
    url: str | None = None,
    user: User = Depends(get_current_user),
):
    """Where Android lands after Readfine is picked out of the share sheet.

    A GET, which is what the manifest declares, because a POST share target arrives
    without the CSRF header the middleware wants and across a SameSite=lax boundary.
    Nothing is saved here: a GET should not change state, and going through the form
    below keeps the rate limit and CSRF checks on the one request that does.

    Logged out, get_current_user redirects to /login and the shared address is lost
    (there is no `next` on that redirect). Known and accepted: an installed app stays
    signed in, and the recovery is to share again.

    Nothing is navigated to afterwards, deliberately. This window holds one history
    entry, so Back returns to whatever was being read when the share started; sending
    it on to /app would put a second entry in the way of that.
    """
    shared_url = extract_shared_url(url, text) or ""
    certain = bool(shared_url) and shared_url_is_certain(url, text)
    return templates.TemplateResponse(request, "app/share_target.html", {
        "shared_title": (title or "").strip()[:_TITLE_MAX],
        "shared_url": shared_url,
        "auto_save": certain and _may_save_without_a_press(request),
        # Kept apart from auto_save, which is also off for a share that arrived by a
        # route we will not save from unasked. Only this one means "we had to choose",
        # and only this one should say so on the page.
        "ambiguous": bool(shared_url) and not certain,
    })


@router.post("/share-target", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_save_url)
async def share_target_save(
    request: Request,
    url: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save the shared address, through the same service the app's own Saved box uses."""
    from app.services.saved_article_service import save_article_by_url

    try:
        article, already_known = await save_article_by_url(url.strip(), user, db)
    except ValueError as exc:
        # Validation-time rejections: a scheme that is not http(s), no host, or an
        # address inside the server's own network. Anything that can only fail once
        # the fetch runs is saved and reported on the article itself.
        #
        # The form comes back with the error rather than the error alone: this may have
        # been sent without anyone pressing anything, and a bare message would leave no
        # way to correct the address it refused. auto_save is off on the way back, so
        # the retry is a press.
        return templates.TemplateResponse(
            request, "app/partials/share_target_form.html",
            {"error": str(exc), "shared_url": url.strip(), "auto_save": False},
        )

    return templates.TemplateResponse(request, "app/partials/share_target_result.html", {
        "article_id": article.id,
        "already_known": already_known,
    })

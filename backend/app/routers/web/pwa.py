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
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

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

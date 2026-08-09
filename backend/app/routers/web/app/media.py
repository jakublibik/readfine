"""Serving media that a stored article references from our own origin.

Right now that is video thumbnails: an article body points its ``<img>`` at
``/img/video-thumb/{provider}/{id}`` instead of at the video host, and this fetches
and caches the image server-side (see :mod:`app.services.video_thumb_service`) so the
reader's browser only ever talks to us. The route is deliberately unauthenticated: a
shared article page (``/share/{token}``) renders video figures for signed-out
readers, and the image must load there too. That is safe because only a fixed pair of
providers and a validated id ever reach the network, so this cannot be turned into an
open proxy; it is rate-limited by IP against plain abuse.
"""
import asyncio
import concurrent.futures

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.config import settings as app_settings_config
from app.rate_limit import limiter

router = APIRouter(tags=["web-app"])

# A dedicated, bounded pool for the blocking thumbnail work. Running it on the shared
# default executor would let a burst of cold thumbnails (each a blocking outbound
# fetch) exhaust that pool and stall unrelated run_in_executor work elsewhere in the
# app; a small fixed pool caps how many fetches can be in flight at once instead.
_THUMB_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="video-thumb"
)

# Immutable: a given video's thumbnail does not change, and the URL carries the id, so
# the browser (and any shared cache such as nginx) may keep it for good and never
# revalidate. This is the header that turns the proxy from a per-view cost into a
# one-fetch-ever one.
_CACHE_CONTROL = "public, max-age=31536000, immutable"


@router.get("/img/video-thumb/{provider}/{vid}")
@limiter.limit(app_settings_config.rate_limit_video_thumb)
async def video_thumbnail(request: Request, provider: str, vid: str):
    """Proxy a video thumbnail, cached on disk, served with a long immutable TTL.

    A 404 covers everything the reader can do nothing about — an id that does not
    validate, a video that is gone, a provider that withholds the thumbnail. The
    ``<img>`` then falls back to its ``alt`` text and the caption's link to the video
    still works, so the article is never broken by a missing thumbnail.
    """
    from app.services.video_thumb_service import get_thumbnail

    etag = f'"vt-{provider}-{vid}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={
            "ETag": etag, "Cache-Control": _CACHE_CONTROL,
        })

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_THUMB_POOL, get_thumbnail, provider, vid)
    if result is None:
        return Response(status_code=404)

    data, mime = result
    return Response(
        content=data,
        media_type=mime,
        headers={"Cache-Control": _CACHE_CONTROL, "ETag": etag},
    )

"""Fetching, caching and serving video thumbnails from our own origin.

A stored article points its video thumbnails at ``/img/video-thumb/{provider}/{id}``
(see :func:`app.utils.video.video_figure`) rather than at img.youtube.com or
vumbnail.com, so opening an article no longer hands a video host the reader's IP and
the video id before any click. This module is the server side of that: it turns a
provider and id into image bytes, going out to fetch them once and keeping them in a
small disk cache so the next reader of the same video costs nothing.

It is only a cache. Deleting the directory loses nothing a re-fetch cannot rebuild,
which is why the size ceiling and the idle sweep may drop entries freely and why
nothing here is tied to the articles table. The fetch itself reuses
:func:`app.utils.url_validator.fetch_url_bytes`, so it inherits SSRF validation on
every redirect hop, IP pinning and the decompressed-size cap without repeating any of
it. Only two providers and a validated id ever reach the network, so this is not a
general fetcher a caller could point anywhere.
"""
import json
import logging
import os
import time
from pathlib import Path

from app.config import settings
from app.utils.url_validator import fetch_url_bytes
from app.utils.video import _VIMEO_ID_RE, _YT_ID_RE

logger = logging.getLogger(__name__)

# A thumbnail fetch should be quick, and a slow host must not tie a cache-serving
# thread up for the full 30 s default. YouTube may try two sizes, so the worst a
# single request costs is twice this.
_FETCH_TIMEOUT = 10

# How long a failed lookup is remembered, so a thumbnail that cannot be had (a deleted
# video sitting in a much-read article) is not fetched again on every open. Kept short
# on purpose: a fetch can also fail on a transient blip, and that video should be able
# to come back on its own before long.
_NEG_CACHE_TTL = 3600

# The magic bytes of the image formats a thumbnail may legitimately be. The response
# is served with X-Content-Type-Options: nosniff (set globally in main.py), so the
# Content-Type has to be right or the browser refuses to render it. The declared type
# is not carried through the disk cache, and a host can mislabel a body anyway, so the
# type is read from the bytes here instead. Anything not on this list is not an image
# and is treated as a failed fetch — it also turns away an HTML error page a host
# might answer with.
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _image_type(data: bytes) -> str | None:
    """The MIME type for *data* by its magic bytes, or None if it is not an image.

    WebP is matched specially: its signature is ``RIFF????WEBP``, four size bytes
    sitting between the two markers.
    """
    for signature, mime in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate(provider: str, vid: str) -> bool:
    """Whether *provider* and *vid* are a shape we will fetch. Nothing else gets out."""
    if provider == "youtube":
        return bool(_YT_ID_RE.match(vid))
    if provider == "vimeo":
        return bool(_VIMEO_ID_RE.match(vid))
    return False


def _cache_dir() -> Path:
    return Path(settings.thumb_cache_dir)


def _cache_path(provider: str, vid: str) -> Path:
    """Where a given thumbnail lives on disk. Inputs are validated before this runs,
    so ``provider`` and ``vid`` cannot carry a path separator into the filename."""
    return _cache_dir() / f"{provider}_{vid}"


def _miss_path(provider: str, vid: str) -> Path:
    """Where the negative-cache marker for a failed lookup lives, next to where its
    thumbnail would have. Same validated inputs, so it too cannot escape the dir."""
    return _cache_dir() / f"{provider}_{vid}.miss"


def _recent_miss(provider: str, vid: str) -> bool:
    """Whether this id failed recently enough that we should not go back out yet.

    Clears the marker once it is past its TTL, so the very next request after that
    tries the host again rather than being stuck on an old failure forever.
    """
    path = _miss_path(provider, vid)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    if age < _NEG_CACHE_TTL:
        return True
    _unlink_quietly(path)
    return False


def _write_miss(provider: str, vid: str) -> None:
    """Remember that this id yielded no thumbnail, so the next request within the TTL
    answers 404 from disk instead of fetching the same failure from the video host."""
    path = _miss_path(provider, vid)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()  # create it, or bump its mtime if a stale marker is still there
    except OSError as exc:
        logger.debug("could not write thumbnail miss marker %s: %s", path, exc)


def _read_cached(path: Path) -> tuple[bytes, str] | None:
    """Return a cached thumbnail and refresh its access time, or None on a miss.

    Touching the access time on a hit is what makes the idle sweep and the LRU
    ceiling measure *use* rather than age: a video that keeps being read stays, one
    whose articles have been purged goes cold and is dropped.
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    mime = _image_type(data)
    if mime is None:
        # A truncated or junk cache file: drop it so the next request re-fetches.
        _unlink_quietly(path)
        return None
    now = time.time()
    try:
        os.utime(path, (now, now))
    except OSError:
        pass
    return data, mime


def _write_cached(path: Path, data: bytes) -> None:
    """Store *data* atomically, then bring the cache back under its size ceiling.

    The write goes to a temp file and is renamed into place so a reader never sees a
    half-written thumbnail, and two requests racing on the same video just overwrite
    each other harmlessly.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        logger.warning("could not write thumbnail cache %s: %s", path, exc)
        return
    _enforce_size_cap()


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _cache_entries() -> list[tuple[Path, os.stat_result]]:
    """Every cache file with its stat, ignoring anything that vanishes mid-scan."""
    entries = []
    try:
        for entry in _cache_dir().iterdir():
            if entry.name.endswith((".tmp", ".miss")):
                continue
            try:
                entries.append((entry, entry.stat()))
            except OSError:
                continue
    except OSError:
        return []
    return entries


def _enforce_size_cap() -> None:
    """Drop least-recently-used thumbnails until the cache fits its byte ceiling."""
    max_bytes = settings.thumb_cache_max_mb * 1024 * 1024
    entries = _cache_entries()
    total = sum(st.st_size for _, st in entries)
    if total <= max_bytes:
        return
    # Oldest access first, so the ones nobody has looked at in longest go first.
    for path, st in sorted(entries, key=lambda e: e[1].st_atime):
        _unlink_quietly(path)
        total -= st.st_size
        if total <= max_bytes:
            break


def _sweep_miss_markers() -> int:
    """Drop negative-cache markers past their TTL. A marker self-expires on the next
    request for the same video (see :func:`_recent_miss`); this is the backstop that
    clears the ones for videos nobody ever asks for again. Independent of the idle
    window because a miss lives for its own short TTL, not the thumbnail's."""
    cutoff = time.time() - _NEG_CACHE_TTL
    removed = 0
    try:
        entries = list(_cache_dir().iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.endswith(".miss"):
            continue
        try:
            stale = entry.stat().st_mtime < cutoff
        except OSError:
            continue
        if stale:
            _unlink_quietly(entry)
            removed += 1
    return removed


def sweep_idle_thumbnails() -> int:
    """Drop thumbnails not requested within the idle window. Returns how many went.

    Run from the scheduler, not on the request path. This is the half of eviction
    that clears entries for articles that have since been purged: nobody opens them,
    so their access time never refreshes, so they age out — no reference from the
    articles table required. Expired negative-cache markers are cleared here too.
    """
    removed = _sweep_miss_markers()
    idle_days = settings.thumb_cache_idle_days
    if idle_days > 0:
        cutoff = time.time() - idle_days * 86400
        for path, st in _cache_entries():
            if st.st_atime < cutoff:
                _unlink_quietly(path)
                removed += 1
    if removed:
        logger.info("swept %d idle video thumbnail(s) from cache", removed)
    return removed


def _fetch_youtube(vid: str) -> bytes | None:
    """The best thumbnail YouTube has for *vid*.

    maxresdefault (1280x720) needs no cropping but 404s on videos uploaded before it
    existed; hqdefault (480x360) is there for every video. So maxres is tried first
    and hqdefault is the fallback, and whichever answers is what gets cached — the
    front end no longer has to try both (it used to, in app.js).
    """
    for name in ("maxresdefault", "hqdefault"):
        url = f"https://img.youtube.com/vi/{vid}/{name}.jpg"
        try:
            return fetch_url_bytes(url, timeout=_FETCH_TIMEOUT).content
        except Exception as exc:  # noqa: BLE001 — any failure just tries the next size
            logger.debug("youtube thumb %s failed: %s", url, exc)
    return None


# The public thumbnail address is not guessable for Vimeo the way it is for YouTube,
# which is the whole reason the old code reached for vumbnail.com. Vimeo's own oEmbed
# endpoint hands it back for public videos with no key, on i.vimeocdn.com — the
# official source, so vumbnail.com is gone.
_VIMEO_OEMBED = "https://vimeo.com/api/oembed.json?url=https://vimeo.com/{vid}"


def _fetch_vimeo(vid: str) -> bytes | None:
    try:
        oembed = fetch_url_bytes(
            _VIMEO_OEMBED.format(vid=vid), timeout=_FETCH_TIMEOUT
        ).content
        thumb_url = json.loads(oembed).get("thumbnail_url")
    except Exception as exc:  # noqa: BLE001
        logger.debug("vimeo oembed for %s failed: %s", vid, exc)
        return None
    if not isinstance(thumb_url, str) or not thumb_url.startswith("https://"):
        return None
    try:
        return fetch_url_bytes(thumb_url, timeout=_FETCH_TIMEOUT).content
    except Exception as exc:  # noqa: BLE001
        logger.debug("vimeo thumb %s failed: %s", thumb_url, exc)
        return None


def get_thumbnail(provider: str, vid: str) -> tuple[bytes, str] | None:
    """Image bytes and MIME type for a video thumbnail, or None if unavailable.

    Cache first; on a miss, fetch from the provider, store, and return. A None means
    the caller should answer 404 — an invalid id, a video that is gone, or a provider
    that would not give the thumbnail up. Synchronous (file IO plus a blocking fetch),
    so the endpoint runs it in a thread.
    """
    if not _validate(provider, vid):
        return None
    path = _cache_path(provider, vid)
    cached = _read_cached(path)
    if cached is not None:
        return cached
    if _recent_miss(provider, vid):
        return None
    data = _fetch_youtube(vid) if provider == "youtube" else _fetch_vimeo(vid)
    mime = _image_type(data) if data else None
    if not data or mime is None:
        _write_miss(provider, vid)
        return None
    _write_cached(path, data)
    return data, mime

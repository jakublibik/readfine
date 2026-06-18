import hashlib
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Per-process cache: asset path → short content hash. Computed lazily on first
# use and held for the process lifetime. A deploy restarts the process, so the
# hash is recomputed and the URL changes whenever the file content changes —
# letting Cloudflare/browsers cache aggressively without manual purges.
_cache: dict[str, str] = {}


def static_url(path: str) -> str:
    """Return a cache-busted URL for a static asset.

    Example: ``static_url('css/tailwind.css')`` → ``/static/css/tailwind.css?v=ab12cd34``.
    If the file can't be read, the bare URL is returned (no version param).
    """
    path = path.lstrip("/")
    version = _cache.get(path)
    if version is None:
        try:
            version = hashlib.sha256((_STATIC_DIR / path).read_bytes()).hexdigest()[:8]
        except OSError:
            version = ""
        _cache[path] = version
    return f"/static/{path}?v={version}" if version else f"/static/{path}"

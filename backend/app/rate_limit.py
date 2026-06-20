import time
from collections import defaultdict
from threading import Lock

from slowapi import Limiter
from starlette.requests import Request

from app.config import settings


def get_client_ip(request: Request) -> str:
    """Resolve the client IP for rate limiting / login lockout.

    Client-supplied forwarding headers (CF-Connecting-IP, X-Forwarded-For) are
    trusted only as far as the deployment's proxy configuration allows; anything
    beyond that is attacker-controlled and ignored. Otherwise the real TCP peer
    is used. See TRUSTED_PROXY_COUNT / TRUST_CLOUDFLARE in config.
    """
    # Cloudflare: trust CF-Connecting-IP only when explicitly enabled. The
    # deployment MUST restrict the origin to Cloudflare IP ranges (firewall),
    # so any request reaching us provably passed through Cloudflare.
    if settings.trust_cloudflare:
        cf_ip = request.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()

    # Reverse proxy: take the entry our own proxy wrote, counting from the
    # RIGHT. With N trusted proxies the real client sits at xff[-N]; everything
    # to its left is client-supplied and ignored. Never the leftmost entry.
    n = settings.trusted_proxy_count
    if n > 0:
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
            if len(parts) >= n:
                return parts[-n]
        # Header missing or shorter than the expected proxy chain → the request
        # did not traverse all trusted hops; fall through to the peer instead.

    # No trusted proxy (default) → real TCP peer. Requires uvicorn NOT to
    # rewrite client.host from forwarded headers (see deploy docs).
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=get_client_ip)


# --- In-memory brute-force tracker for login ---

_LOCKOUT_THRESHOLD = 10   # failed attempts before lockout
_LOCKOUT_SECONDS = 900    # 15 minutes

_lock = Lock()
# key: (ip, email) → {"count": int, "locked_until": float | None, "last_attempt": float}
_failed_attempts: dict[tuple[str, str], dict] = defaultdict(
    lambda: {"count": 0, "locked_until": None, "last_attempt": 0.0}
)


def check_login_lockout(ip: str, email: str) -> bool:
    """Return True if this (ip, email) combination is currently locked out."""
    key = (ip, email.lower())
    with _lock:
        entry = _failed_attempts.get(key)
        if entry is None:
            return False
        if entry["locked_until"] and time.monotonic() < entry["locked_until"]:
            return True
        return False


def record_failed_login(ip: str, email: str) -> bool:
    """Record a failed login attempt. Returns True if lockout was just triggered."""
    key = (ip, email.lower())
    with _lock:
        entry = _failed_attempts[key]
        entry["count"] += 1
        entry["last_attempt"] = time.monotonic()
        if entry["count"] >= _LOCKOUT_THRESHOLD:
            entry["locked_until"] = time.monotonic() + _LOCKOUT_SECONDS
            return True
        return False


def clear_failed_logins(ip: str, email: str) -> None:
    """Clear failed attempts on successful login."""
    key = (ip, email.lower())
    with _lock:
        _failed_attempts.pop(key, None)

"""URL validation with SSRF protection."""
import asyncio
import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlparse

import httpx

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 10

# HTTP statuses that signal a *transient* failure (rate limit / timeout) rather
# than a permanent one. Feeds returning these back off instead of being disabled.
TRANSIENT_HTTP_STATUSES = frozenset({408, 429})

# Bounds for an honored Retry-After delay: never retry sooner than this, never
# wait longer than this regardless of what the server asks for.
_RETRY_AFTER_MIN = timedelta(seconds=60)
_RETRY_AFTER_MAX = timedelta(hours=24)


def parse_retry_after(value: str | None, now: datetime) -> datetime | None:
    """Parse an HTTP ``Retry-After`` header into an absolute UTC timestamp.

    Accepts either delta-seconds (RFC 7231) or an HTTP-date. The resulting delay
    is clamped to ``[_RETRY_AFTER_MIN, _RETRY_AFTER_MAX]``. Returns ``None`` for
    missing/blank/invalid input or a date that is not in the future.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None

    delay: timedelta | None = None
    if value.isdigit():
        delay = timedelta(seconds=int(value))
    else:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay = parsed - now

    if delay is None or delay <= timedelta(0):
        return None
    delay = max(_RETRY_AFTER_MIN, min(delay, _RETRY_AFTER_MAX))
    return now + delay


def redact_url(url: str) -> str:
    """Strip credentials and query string from a URL for safe logging.

    Feed/scrape URLs may carry secrets in the query string (e.g. ``?api_key=...``)
    or HTTP credentials in the netloc (``user:pass@host``). Keep scheme, host,
    port and path; replace any query with a redaction marker. Returns the input
    unchanged if it doesn't look like a URL, or a marker if it can't be parsed.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "<unparseable-url>"
    if not parsed.scheme and not parsed.netloc:
        return url
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    cleaned = f"{parsed.scheme}://{host}{parsed.path}" if parsed.scheme else f"{host}{parsed.path}"
    if parsed.query:
        cleaned += "?<redacted>"
    return cleaned


def validate_feed_url(url: str) -> None:
    """
    Validate a feed URL for use in server-side HTTP requests.

    Raises ValueError if:
    - scheme is not http/https
    - hostname resolves to a private/loopback/link-local address

    Known limitation (DNS rebinding / TOCTOU): this resolves DNS to check the
    IP, but httpx re-resolves at connect time, so a hostname whose DNS flips
    between calls could pass validation yet connect to a private/metadata IP.
    Low severity (needs attacker-controlled authoritative DNS + race). Deferred
    hardening (review M4): pin the validated IP and connect to it directly.
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}': only http and https are allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname '{hostname}': {exc}") from exc

    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(
                f"URL resolves to a disallowed address ({ip}): "
                "localhost, private, and link-local addresses are not permitted"
            )


async def async_validate_feed_url(url: str) -> None:
    """Async wrapper around validate_feed_url — offloads blocking DNS lookup to executor."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, validate_feed_url, url)


def fetch_url_with_ssrf_check(
    url: str,
    auth=None,
    timeout: int = 30,
    headers: dict | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> str:
    """Synchronous HTTP fetch with SSRF-safe redirect validation on every hop."""
    current_url = url
    with httpx.Client(timeout=timeout, follow_redirects=False, auth=auth, headers=headers) as client:
        for _ in range(max_redirects + 1):
            response = client.get(current_url)
            if not response.is_redirect:
                response.raise_for_status()
                return response.text
            redirect_url = response.headers.get("location", "")
            if redirect_url and not redirect_url.startswith(("http://", "https://")):
                redirect_url = urljoin(current_url, redirect_url)
            validate_feed_url(redirect_url)
            current_url = redirect_url
    raise httpx.TooManyRedirects(
        f"Too many redirects (max {max_redirects})", request=response.request
    )

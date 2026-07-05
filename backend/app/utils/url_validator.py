"""URL validation with SSRF protection."""
import asyncio
import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 10

# HTTP statuses that carry rate-limit / timeout semantics: we read their
# Retry-After / RateLimit-* headers and arm the per-host cooldown from them.
TRANSIENT_HTTP_STATUSES = frozenset({408, 429})

# 4xx statuses that should NOT disable a feed on the first hit; instead the feed
# backs off through the error tier and is disabled only after
# FETCH_ERROR_DISABLE_THRESHOLD consecutive failures. Superset of the rate-limit
# statuses plus 403: Reddit/YouTube return 403 as a transient anti-bot /
# rate-adjacent block (datacenter IP, generic UA) far more often than as a
# permanent denial, so retrying beats disabling on a single 403.
RETRYABLE_HTTP_STATUSES = TRANSIENT_HTTP_STATUSES | {403}

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


# Bounds for a rate-limit reset delay. Unlike Retry-After we do NOT force a 60s
# floor — the bounded-hybrid scheduler wants the true (possibly small) value so a
# short reset can be waited out in-round.
_RATE_LIMIT_MAX = timedelta(hours=24)
# A *-reset value above this is treated as an absolute unix timestamp (epoch
# seconds) rather than delta-seconds. ~1e9 is 2001; any real "seconds until reset"
# is far smaller, so this cleanly separates the two conventions.
_EPOCH_THRESHOLD = 1_000_000_000

_REMAINING_HEADERS = ("ratelimit-remaining", "x-ratelimit-remaining", "x-rate-limit-remaining")
_RESET_HEADERS = ("ratelimit-reset", "x-ratelimit-reset", "x-rate-limit-reset")


def _first_header(headers, names: tuple[str, ...]) -> str | None:
    """Return the first present header value among *names* (case-insensitive)."""
    for name in names:
        value = headers.get(name)
        if value is not None:
            return value
    return None


def _as_float(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def rate_limited_until(headers, now: datetime) -> datetime | None:
    """Compute when a host next allows a request, from rate-limit response headers.

    Precedence:
      1. ``Retry-After`` (authoritative; sent on 429/503) — via ``parse_retry_after``.
      2. A ``*-remaining`` header at/below zero → wait ``*-reset`` seconds. Supports
         the IETF ``RateLimit-*`` draft and legacy ``X-RateLimit-*`` / ``X-Rate-Limit-*``
         spellings. ``*-reset`` is delta-seconds unless it looks like a unix epoch.

    Returns an absolute UTC timestamp, or ``None`` when no rate-limit signal is
    present (e.g. remaining header absent, or remaining still > 0). The result is
    clamped to at most ``_RATE_LIMIT_MAX`` in the future.
    """
    retry_after = parse_retry_after(headers.get("retry-after"), now)
    if retry_after is not None:
        return retry_after

    remaining = _as_float(_first_header(headers, _REMAINING_HEADERS))
    if remaining is None or remaining > 0:
        return None

    reset = _as_float(_first_header(headers, _RESET_HEADERS))
    if reset is None:
        return None
    if reset >= _EPOCH_THRESHOLD:
        until = datetime.fromtimestamp(reset, tz=timezone.utc)
        if until <= now:
            return None
    else:
        if reset <= 0:
            return None
        until = now + timedelta(seconds=reset)
    return min(until, now + _RATE_LIMIT_MAX)


def format_retry_in(until: datetime, now: datetime) -> str:
    """Human 'try again in …' phrase for a cooldown expiry.

    Rate-limit resets are often just seconds (Reddit's ``x-ratelimit-reset``), so
    show seconds under ~90s and minutes above. Callers embed this in a 429 message.
    """
    secs = max(1, round((until - now).total_seconds()))
    return f"{secs} sec" if secs < 90 else f"about {round(secs / 60)} min"


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


def _resolve_response(
    url: str,
    auth=None,
    timeout: int = 30,
    headers: dict | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> httpx.Response:
    """Fetch a URL following redirects, validating every hop against SSRF.

    Returns the final response after ``raise_for_status()``. A 304 Not Modified is
    returned without raising (httpx classifies 304 as a redirect status yet it has
    no ``Location``, so it is treated as a terminal response here, for conditional
    requests).
    """
    current_url = url
    with httpx.Client(timeout=timeout, follow_redirects=False, auth=auth, headers=headers) as client:
        for _ in range(max_redirects + 1):
            response = client.get(current_url)
            # Only an actual redirect (3xx with a Location) is followed; 304 has a
            # redirect-class status but no Location, so it falls through as terminal.
            if not response.has_redirect_location:
                if response.status_code != 304:
                    response.raise_for_status()
                return response
            redirect_url = response.headers.get("location", "")
            if redirect_url and not redirect_url.startswith(("http://", "https://")):
                redirect_url = urljoin(current_url, redirect_url)
            validate_feed_url(redirect_url)
            current_url = redirect_url
    raise httpx.TooManyRedirects(
        f"Too many redirects (max {max_redirects})", request=response.request
    )


def fetch_url_with_ssrf_check(
    url: str,
    auth=None,
    timeout: int = 30,
    headers: dict | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> str:
    """Synchronous HTTP fetch with SSRF-safe redirect validation on every hop."""
    return _resolve_response(url, auth, timeout, headers, max_redirects).text


class ConditionalResponse(NamedTuple):
    """Result of a conditional HTTP fetch.

    ``status_code`` is 304 when the server reports the resource is unchanged (in
    which case ``text`` is empty); otherwise 200 with the body. ``etag`` and
    ``last_modified`` are the validators returned by the server, to be stored and
    sent back on the next fetch.
    """
    status_code: int
    text: str
    etag: str | None
    last_modified: str | None
    # When set, the host asked us to hold off until this UTC instant (derived from
    # Retry-After / RateLimit-* headers). None when the response carried no such signal.
    rate_limited_until: datetime | None = None


def fetch_url_conditional(
    url: str,
    auth=None,
    timeout: int = 30,
    headers: dict | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    max_redirects: int = _MAX_REDIRECTS,
) -> ConditionalResponse:
    """SSRF-safe fetch that sends conditional-request validators.

    When ``etag``/``last_modified`` are provided they are sent as ``If-None-Match``
    / ``If-Modified-Since``; an unchanged resource then answers ``304 Not Modified``
    with no body. 4xx/5xx (including 429) still raise via ``raise_for_status()``.
    """
    request_headers = dict(headers or {})
    if etag:
        request_headers["If-None-Match"] = etag
    if last_modified:
        request_headers["If-Modified-Since"] = last_modified
    response = _resolve_response(url, auth, timeout, request_headers, max_redirects)
    new_etag = response.headers.get("etag")
    new_last_modified = response.headers.get("last-modified")
    return ConditionalResponse(
        status_code=response.status_code,
        text=response.text,
        etag=new_etag[:255] if new_etag else None,
        last_modified=new_last_modified[:255] if new_last_modified else None,
        rate_limited_until=rate_limited_until(response.headers, datetime.now(timezone.utc)),
    )

"""URL validation with SSRF protection."""
import asyncio
import ipaddress
import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import NamedTuple
from urllib.parse import urljoin, urlparse

import httpx

logger = logging.getLogger(__name__)

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


def spacing_from_headers(headers, now: datetime) -> float | None:
    """Derive a sustainable per-request spacing (seconds) from ``RateLimit-*`` headers.

    Unlike :func:`rate_limited_until` (which fires only when the budget is exhausted),
    this reads the *live* allowance on any response — ``spacing = reset / remaining``,
    i.e. the seconds to spread the remaining calls evenly over the remaining window.
    A host reporting ``remaining=10, reset=60`` yields 6s; ``remaining=1, reset=60``
    yields 60s. Returns ``None`` when the headers are absent or unusable.

    ``*-reset`` is delta-seconds unless it looks like a unix epoch (same convention as
    :func:`rate_limited_until`). ``remaining`` is floored to 1 so an exhausted budget
    maps to the full reset window rather than dividing by zero.
    """
    reset = _as_float(_first_header(headers, _RESET_HEADERS))
    remaining = _as_float(_first_header(headers, _REMAINING_HEADERS))
    if reset is None or remaining is None:
        return None
    seconds = (reset - now.timestamp()) if reset >= _EPOCH_THRESHOLD else reset
    if seconds <= 0:
        return None
    spacing = seconds / max(remaining, 1.0)
    return spacing if spacing > 0 else None


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


# Rate-limit headers worth recording, in the order they are logged. Each entry is
# (label, candidate header names) — hosts disagree on the prefix (Reddit sends the
# x- variants, RFC 9239 drops it), so both spellings are tried.
_OUTBOUND_LOG_HEADERS = (
    ("retry_after", ("retry-after",)),
    ("rl_used", ("x-ratelimit-used", "ratelimit-used")),
    ("rl_remaining", ("x-ratelimit-remaining", "ratelimit-remaining")),
    ("rl_reset", ("x-ratelimit-reset", "ratelimit-reset")),
    ("rl_limit", ("x-ratelimit-limit", "ratelimit-limit")),
)


def log_outbound(
    url: str, response: httpx.Response | None, started: float, error: str | None = None
) -> None:
    """Record one outbound HTTP request when ``LOG_OUTBOUND_REQUESTS`` is enabled.

    Per-feed error records cannot answer "how often do we actually hit this host":
    a host is shared by several feeds and by readable extraction, and successful
    requests leave no trace at all. This logs every attempt with its timestamp,
    status and rate-limit headers, so the real request rate and spacing per host
    can be reconstructed from the log.

    *started* is a ``time.monotonic()`` reading taken just before the request.
    *url* is the logical URL (the hostname one), not the pinned-IP connect URL.
    """
    from app.config import settings

    if not settings.log_outbound_requests:
        return
    try:
        parts = [f"host={urlparse(url).hostname or '?'}"]
        if response is not None:
            parts.append(f"status={response.status_code}")
            parts.append(f"http={response.http_version}")
        else:
            parts.append(f"status=ERR({error or 'unknown'})")
        parts.append(f"ms={int((time.monotonic() - started) * 1000)}")
        if response is not None:
            for label, names in _OUTBOUND_LOG_HEADERS:
                value = _first_header(response.headers, names)
                if value is not None:
                    parts.append(f"{label}={value}")
        parts.append(f"url={redact_url(url)}")
        logger.info("outbound %s", " ".join(parts))
    except Exception:  # diagnostics must never break a fetch
        logger.debug("outbound request logging failed", exc_info=True)


def _resolve_and_pin(hostname: str) -> str:
    """Resolve *hostname* once, reject it if any resolved address is disallowed
    (loopback/private/link-local/reserved/multicast), and return a single public
    IP to pin the connection to. Raises ValueError on resolution failure or a
    disallowed address.

    Pinning the connect target to the IP we validated is what closes the
    DNS-rebinding / TOCTOU window: without it the OS re-resolves at connect time
    and an attacker-controlled authoritative DNS could hand back a private /
    metadata address (169.254.169.254, …) different from the one we checked.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve hostname '{hostname}': {exc}") from exc

    pinned: str | None = None
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
        if pinned is None:
            pinned = ip_str
    if pinned is None:
        raise ValueError(f"Cannot resolve hostname '{hostname}': no usable address")
    return pinned


def validate_feed_url(url: str) -> None:
    """
    Validate a feed URL for use in server-side HTTP requests.

    Raises ValueError if:
    - scheme is not http/https
    - hostname resolves to a private/loopback/link-local address

    This is the pre-flight check used by callers that validate a URL without
    fetching. The fetch path (:func:`_resolve_response`) additionally pins the
    connection to the validated IP, so DNS cannot rebind to a private/metadata
    address between this check and the connect (review M4).
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}': only http and https are allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    _resolve_and_pin(hostname)


def _pin_connection(url: str) -> tuple[str, dict[str, str], dict]:
    """Validate *url* and rewrite it to connect to a pinned, validated IP.

    Returns ``(connect_url, header_overlay, extensions)``:
      * ``connect_url`` has its host replaced by the validated IP (userinfo and
        port preserved), so the socket connects to exactly the address we checked
        rather than a re-resolved one.
      * ``header_overlay`` carries the original ``Host`` so name-based virtual
        hosting still works.
      * ``extensions`` sets ``sni_hostname`` for HTTPS so TLS SNI and certificate
        verification run against the real hostname, not the IP.
    """
    parsed = urlparse(url)

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"Invalid URL scheme '{parsed.scheme}': only http and https are allowed")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("URL has no hostname")

    ip = _resolve_and_pin(hostname)

    ip_netloc = f"[{ip}]" if ":" in ip else ip
    if parsed.port:
        ip_netloc = f"{ip_netloc}:{parsed.port}"
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    connect_url = parsed._replace(netloc=userinfo + ip_netloc).geturl()

    host_header = f"{hostname}:{parsed.port}" if parsed.port else hostname
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
    return connect_url, {"Host": host_header}, extensions


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
    # http2=True: negotiate HTTP/2 when the server offers it (falls back to HTTP/1.1
    # otherwise). Some CDNs treat a plain HTTP/1.1 request as a bot signal and answer
    # with a header-less 403 / near-zero rate budget (observed on Reddit via Fastly),
    # while serving HTTP/2 clients normally.
    with httpx.Client(
        timeout=timeout, follow_redirects=False, auth=auth, headers=headers, http2=True
    ) as client:
        for _ in range(max_redirects + 1):
            # Validate + pin every hop to its resolved IP; connecting to the IP
            # (with the original Host header and HTTPS SNI) removes the re-resolve
            # that would otherwise reopen the DNS-rebinding window.
            connect_url, host_overlay, extensions = _pin_connection(current_url)
            started = time.monotonic()
            try:
                response = client.get(connect_url, headers=host_overlay, extensions=extensions)
            except Exception as exc:
                log_outbound(current_url, None, started, error=type(exc).__name__)
                raise
            log_outbound(current_url, response, started)
            # Only an actual redirect (3xx with a Location) is followed; 304 has a
            # redirect-class status but no Location, so it falls through as terminal.
            if not response.has_redirect_location:
                if response.status_code != 304:
                    response.raise_for_status()
                return response
            redirect_url = response.headers.get("location", "")
            if redirect_url and not redirect_url.startswith(("http://", "https://")):
                redirect_url = urljoin(current_url, redirect_url)
            # The next iteration's _pin_connection validates redirect_url before use.
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
    # Sustainable per-request spacing (seconds) advertised by live RateLimit-* headers
    # (reset / remaining), for learned per-host pacing. None when not advertised.
    spacing_seconds: float | None = None


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
    now = datetime.now(timezone.utc)
    return ConditionalResponse(
        status_code=response.status_code,
        text=response.text,
        etag=new_etag[:255] if new_etag else None,
        last_modified=new_last_modified[:255] if new_last_modified else None,
        rate_limited_until=rate_limited_until(response.headers, now),
        spacing_seconds=spacing_from_headers(response.headers, now),
    )

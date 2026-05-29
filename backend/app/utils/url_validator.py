"""URL validation with SSRF protection."""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 10


def validate_feed_url(url: str) -> None:
    """
    Validate a feed URL for use in server-side HTTP requests.

    Raises ValueError if:
    - scheme is not http/https
    - hostname resolves to a private/loopback/link-local address
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

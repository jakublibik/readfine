"""URL validation with SSRF protection."""
import ipaddress
import socket
from urllib.parse import urlparse


_ALLOWED_SCHEMES = {"http", "https"}
_MAX_REDIRECTS = 5


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

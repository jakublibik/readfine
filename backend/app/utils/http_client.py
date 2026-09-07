"""Shared HTTP client constants and helpers."""
import httpx

READFINE_UA = "Readfine/1.0 (self-hosted RSS reader)"

# Reason phrases httpx has no entry for, because these codes are vendor extensions
# rather than IANA-registered. Without them a failure message is the bare number and
# nothing else — "HTTP 520" was the whole of what the admin dashboard could say about
# a feed behind Cloudflare, which is the range that hits feed fetching most often.
_EXTRA_REASON_PHRASES = {
    420: "Enhance Your Calm",  # Twitter-era rate limit, still sent by a few hosts
    444: "No Response",  # nginx, connection closed without a reply
    494: "Request Header Too Large",  # nginx
    495: "SSL Certificate Error",  # nginx
    496: "SSL Certificate Required",  # nginx
    497: "HTTP Request Sent to HTTPS Port",  # nginx
    499: "Client Closed Request",  # nginx
    509: "Bandwidth Limit Exceeded",  # Apache/cPanel, shared hosting over quota
    # Cloudflare's 52x range: the CDN answered, the origin behind it did not.
    520: "Web Server Returned an Unknown Error",
    521: "Web Server Is Down",
    522: "Connection Timed Out",
    523: "Origin Is Unreachable",
    524: "A Timeout Occurred",
    525: "SSL Handshake Failed",
    526: "Invalid SSL Certificate",
    527: "Railgun Error",
    530: "Origin Error",
    999: "Request Denied",  # LinkedIn and a handful of other anti-bot front ends
}


def http_reason(status_code: int) -> str:
    """The reason phrase for a status code, non-standard ones included.

    ``httpx`` knows the registered codes only and returns "" for everything else, so
    a message built from it read "HTTP 520" with nothing after it. httpx keeps
    precedence; our table only fills the gaps. Still "" for a code nobody has a name
    for, so callers should build the label with ``rstrip()`` or a conditional.
    """
    return httpx.codes.get_reason_phrase(status_code) or _EXTRA_REASON_PHRASES.get(status_code, "")

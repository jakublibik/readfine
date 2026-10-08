"""What went wrong with a fetch someone is waiting on, in words they can act on.

Subscribing, testing a feed and changing a feed's address all fetch the address
while the user watches, and each used to turn the failure into a sentence on its
own. The web form had one wording per HTTP status, the test button another, the
API none at all (a 404 there came back as a 500). This is the one place that
classifies the failure, so every caller says the same thing and decides what to
offer next (feed detection, a scrape setup) by ``kind`` rather than by matching
words in the message.

The scheduled fetcher does not use it: a background failure goes on the feed row
through :mod:`app.fetcher.failure`, where the counters and backoff matter more
than the wording.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.utils.http_client import http_reason
from app.utils.url_validator import ResponseTooLarge, format_retry_in, rate_limited_until


class NotAFeed(ValueError):
    """The address answered, but not with an RSS/Atom feed (usually a web page).

    A ``ValueError`` like the other source problems, so the fetcher's failure
    classification treats it as the feed's fault, not ours.
    """


@dataclass(frozen=True)
class FetchProblem:
    """A classified fetch failure.

    ``kind`` is one of ``http`` (the server answered with an error status),
    ``timeout``, ``transport`` (no usable answer at all), ``too_large``,
    ``not_feed`` (an answer, but not a feed) and ``invalid`` (we refused the
    address, or it cannot be resolved). ``status`` is set for ``http`` only.
    """
    kind: str
    status: int | None
    message: str

    @property
    def wrong_kind_of_page(self) -> bool:
        """Could the address be a web page rather than a feed?

        True when it is worth looking for a feed linked from the page or offering
        to scrape it. Not for a login wall, a rate limit or a server that is down:
        another request to the same host would only make those worse, and the page
        is no more scrapable than the feed was.
        """
        if self.kind == "not_feed":
            return True
        return (
            self.kind == "http"
            and self.status is not None
            and self.status not in (401, 429)
            and self.status < 500
        )


def describe_fetch_error(exc: Exception) -> FetchProblem | None:
    """Classify *exc* from fetching a feed or page address, or None when it is not
    a fetch failure at all (a bug of ours, which the caller should let propagate)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return FetchProblem("http", exc.response.status_code, _status_message(exc.response))
    if isinstance(exc, httpx.TimeoutException):
        return FetchProblem(
            "timeout", None,
            "The server took too long to respond. It may be down or slow, try again later.",
        )
    if isinstance(exc, (httpx.RemoteProtocolError, httpx.ReadError)):
        # Dropped before any HTTP status. CDNs like Cloudflare do this to throttle
        # datacenter IPs instead of returning a 429, so it is not a bad address.
        return FetchProblem(
            "transport", None,
            "The server closed the connection without responding. It is likely blocking "
            "or rate-limiting requests from this host (common for datacenter IPs). "
            "Try again later.",
        )
    if isinstance(exc, httpx.HTTPError):
        return FetchProblem("transport", None, f"Could not connect to the server: {exc}")
    if isinstance(exc, ResponseTooLarge):
        return FetchProblem("too_large", None, f"{exc}.")
    if isinstance(exc, NotAFeed):
        return FetchProblem("not_feed", None, str(exc))
    if isinstance(exc, ValueError):
        # The address validator's own sentences: a blocked address or redirect, a
        # hostname that does not resolve.
        return FetchProblem("invalid", None, str(exc))
    return None


def _status_message(response: httpx.Response) -> str:
    status = response.status_code
    if status == 401:
        return ("Authentication required (401). Add HTTP credentials, or check the ones "
                "you entered.")
    if status == 403:
        return ("Access denied (403). The server is likely blocking requests from this "
                "host (geo-block or datacenter IP block).")
    if status == 404:
        return "Not found (404). The address may no longer exist."
    if status == 429:
        # Reads Retry-After and x-ratelimit-reset (Reddit sends the latter, no
        # Retry-After). Resets are often seconds, so show seconds under ~90s.
        now = datetime.now(timezone.utc)
        until = rate_limited_until(response.headers, now)
        wait = format_retry_in(until, now) if until is not None else "a few minutes"
        return f"Too many requests (429). The server is rate-limiting this host. Try again in {wait}."
    if 500 <= status < 600:
        return (f"The server returned an error ({status}). It may be temporarily down, "
                "try again later.")
    return f"The server returned HTTP {status} {http_reason(status)}".rstrip() + "."

"""The ``?next=`` target of the sign-in page: where to land after logging in."""
from urllib.parse import urlsplit


def safe_next_path(value: str | None) -> str | None:
    """``value`` if it is a path on this site, else None.

    Only a plain local path passes, so the parameter cannot be turned into an open
    redirect: ``//evil.test`` and ``/\\evil.test`` are read by browsers as another
    host, and an absolute URL is one outright. Control characters are refused too,
    since some browsers strip them before parsing the location.
    """
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return None
    if "\\" in value or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value

"""Symmetric encryption helpers for sensitive fields (e.g. feed auth passwords).

Also home to the rule for when two half-credentials add up to an HTTP Basic pair
(:func:`auth_pair`), because :func:`feed_auth` is the version everything already
cites and the two must not answer differently.
"""
import base64
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

logger = logging.getLogger(__name__)


def _get_fernet() -> Fernet:
    key = settings.encryption_key.encode()
    # Accept raw 32-byte keys and also pre-encoded Fernet keys (44 chars)
    if len(key) == 32:
        key = base64.urlsafe_b64encode(key)
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns a Fernet token (str)."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a Fernet token. Raises ValueError on invalid token."""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Failed to decrypt value") from exc


def auth_pair(auth_user: str | None, auth_pass: str | None) -> tuple[str, str] | None:
    """The HTTP Basic pair *auth_user* and *auth_pass* make, or None if they make none.

    Both halves must be **present**, and present means non-NULL rather than non-empty.
    Credentials lifted out of a URL keep the shape they had there, including the empty
    password of ``https://user@host/feed``, which is what httpx put on the wire while
    the address was stored whole. A blank field in the HTTP auth form is stored as
    NULL, so form-entered credentials are unaffected by the distinction.

    Written once because the two halves reach a fetch from several directions (the
    auth form, an address the user pasted, two columns of a feed row) and the truthy
    version of this test silently drops the empty-password pair. Callers holding an
    encrypted password want :func:`feed_auth` instead, which is this plus the decrypt.
    """
    if auth_user is None or auth_pass is None:
        return None
    return auth_user, auth_pass


def feed_auth(
    auth_user: str | None, auth_pass_enc: str | None, *, context: str = ""
) -> tuple[str, str] | None:
    """The HTTP Basic pair stored on a feed, or None when it has none.

    Every path that fetches on a feed's behalf (the RSS and scrape fetchers, readable
    extraction from the scheduler and from an opened article) needs this same
    decrypt-and-pair step, so it lives in one place. What counts as a pair is
    :func:`auth_pair` above, shared with the paths whose password never was encrypted.

    A password that will not decrypt (a rotated or corrupted ``ENCRYPTION_KEY``) is
    logged against *context* and reported as no credentials, so one unreadable row
    cannot take down the fetch around it.
    """
    if auth_user is None or auth_pass_enc is None:
        return None
    try:
        return auth_pair(auth_user, decrypt(auth_pass_enc))
    except ValueError as exc:
        logger.warning(
            "Failed to decrypt fetch_auth_pass%s: %s — fetching without credentials",
            f" for {context}" if context else "", exc,
        )
        return None

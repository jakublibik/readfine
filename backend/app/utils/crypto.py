"""Symmetric encryption helpers for sensitive fields (e.g. feed auth passwords)."""
import base64

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


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

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from jose import JWTError, jwt

from app.config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60  # short-lived interactive bearer; long-lived access = ApiToken


MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates input beyond this


def password_within_limit(password: str) -> bool:
    """True if the password fits bcrypt's 72-byte input limit (UTF-8 encoded).

    Callers validate with this first to surface a friendly message; hash_password
    enforces it as a backstop.
    """
    return len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES


def hash_password(password: str) -> str:
    if not password_within_limit(password):
        # Refuse rather than let bcrypt truncate — otherwise two different long
        # passwords sharing a 72-byte prefix would be accepted interchangeably.
        raise ValueError("Password exceeds bcrypt's 72-byte limit")
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


# A valid bcrypt hash (default cost) used to equalize login response timing when
# the email doesn't exist. Without a real verify, the no-user path returns
# noticeably faster and leaks account existence. Generated once at import so the
# cost factor always matches gensalt().
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(b"timing-equalizer", bcrypt.gensalt()).decode()


def dummy_verify_password() -> None:
    """Throwaway bcrypt verify to keep unknown-user logins constant-time."""
    bcrypt.checkpw(b"invalid", _DUMMY_PASSWORD_HASH.encode())


def create_access_token(user_id: int, role: str, token_version: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role, "tv": token_version, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


def generate_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """SHA-256 hash for storing API tokens and reset tokens."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()

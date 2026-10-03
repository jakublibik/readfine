import asyncio
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

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


# bcrypt is slow on purpose (~0.2 s), and the app runs a single worker: on the event
# loop, every sign-up or login would stall all other requests for that long, so a
# burst of sign-ups froze the whole instance for seconds. bcrypt releases the GIL,
# so in a thread it runs alongside the loop. Used on the public endpoints, where a
# burst can come from outside.
async def hash_password_async(password: str) -> str:
    return await asyncio.get_running_loop().run_in_executor(None, hash_password, password)


async def verify_password_async(password: str, password_hash: str | None) -> bool:
    """`verify_password` off the event loop. With no hash (unknown user) it runs the
    dummy verify, so the answer takes as long either way, and returns False."""
    loop = asyncio.get_running_loop()
    if password_hash is None:
        await loop.run_in_executor(None, dummy_verify_password)
        return False
    return await loop.run_in_executor(None, verify_password, password, password_hash)


def create_access_token(user_id: int, role: str, token_version: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {"sub": str(user_id), "role": role, "tv": token_version, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except jwt.PyJWTError:
        return None


def generate_token(length: int = 32) -> str:
    return secrets.token_urlsafe(length)


def hash_token(token: str) -> str:
    """SHA-256 hash for storing API tokens and reset tokens."""
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()

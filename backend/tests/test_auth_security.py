"""Unit tests for auth security utilities."""
import pytest
from datetime import datetime, timezone

from app.auth.security import (
    hash_password,
    verify_password,
    create_access_token,
    decode_access_token,
    generate_token,
    hash_token,
)


# ── Password hashing ─────────────────────────────────────────────────────────

class TestHashPassword:
    def test_returns_string(self):
        h = hash_password("secret123")
        assert isinstance(h, str)

    def test_not_plaintext(self):
        h = hash_password("secret123")
        assert h != "secret123"

    def test_bcrypt_prefix(self):
        h = hash_password("secret123")
        assert h.startswith("$2b$")

    def test_different_hashes_same_password(self):
        """bcrypt uses random salt, so two hashes of the same password must differ."""
        h1 = hash_password("secret123")
        h2 = hash_password("secret123")
        assert h1 != h2


class TestVerifyPassword:
    def test_correct_password(self):
        h = hash_password("correct_horse")
        assert verify_password("correct_horse", h) is True

    def test_wrong_password(self):
        h = hash_password("correct_horse")
        assert verify_password("wrong_horse", h) is False

    def test_empty_password_fails(self):
        h = hash_password("nonempty")
        assert verify_password("", h) is False

    def test_case_sensitive(self):
        h = hash_password("Secret")
        assert verify_password("secret", h) is False


# ── JWT tokens ───────────────────────────────────────────────────────────────

class TestAccessToken:
    def test_create_and_decode_roundtrip(self):
        token = create_access_token(user_id=42, role="user", token_version=0)
        payload = decode_access_token(token)
        assert payload is not None
        assert payload["sub"] == "42"
        assert payload["role"] == "user"

    def test_admin_role_preserved(self):
        token = create_access_token(user_id=1, role="admin", token_version=0)
        payload = decode_access_token(token)
        assert payload["role"] == "admin"

    def test_invalid_token_returns_none(self):
        assert decode_access_token("not.a.valid.token") is None

    def test_tampered_token_returns_none(self):
        token = create_access_token(user_id=1, role="user", token_version=0)
        tampered = token[:-5] + "XXXXX"
        assert decode_access_token(tampered) is None

    def test_token_has_expiry(self):
        token = create_access_token(user_id=1, role="user", token_version=0)
        payload = decode_access_token(token)
        assert "exp" in payload
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        assert exp > datetime.now(timezone.utc)

    def test_token_carries_version(self):
        token = create_access_token(user_id=1, role="user", token_version=7)
        payload = decode_access_token(token)
        assert payload["tv"] == 7


# ── Token utilities ──────────────────────────────────────────────────────────

class TestGenerateToken:
    def test_returns_string(self):
        assert isinstance(generate_token(), str)

    def test_default_length_reasonable(self):
        # token_urlsafe(32) → ~43 chars
        t = generate_token(32)
        assert len(t) >= 30

    def test_unique(self):
        tokens = {generate_token() for _ in range(20)}
        assert len(tokens) == 20


class TestHashToken:
    def test_returns_hex_string(self):
        h = hash_token("some_token")
        assert all(c in "0123456789abcdef" for c in h)

    def test_sha256_length(self):
        h = hash_token("anything")
        assert len(h) == 64

    def test_deterministic(self):
        assert hash_token("abc") == hash_token("abc")

    def test_different_inputs_differ(self):
        assert hash_token("token_a") != hash_token("token_b")

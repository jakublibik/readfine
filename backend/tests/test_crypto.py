"""Unit tests for crypto utilities (Fernet encrypt/decrypt)."""
import pytest
from cryptography.fernet import Fernet

from app.utils.crypto import decrypt, encrypt


class TestEncryptDecrypt:
    def test_round_trip(self):
        plaintext = "super-secret-password"
        assert decrypt(encrypt(plaintext)) == plaintext

    def test_empty_string(self):
        assert decrypt(encrypt("")) == ""

    def test_unicode(self):
        value = "héslo123 ❤️"
        assert decrypt(encrypt(value)) == value

    def test_long_value(self):
        value = "x" * 10_000
        assert decrypt(encrypt(value)) == value

    def test_ciphertext_differs_from_plaintext(self):
        plaintext = "password"
        assert encrypt(plaintext) != plaintext

    def test_two_encryptions_differ(self):
        # Fernet uses random IV → same input produces different tokens
        plaintext = "password"
        assert encrypt(plaintext) != encrypt(plaintext)

    def test_decrypt_wrong_key_raises(self):
        plaintext = "secret"
        ciphertext = encrypt(plaintext)

        # Create a token encrypted with a different key
        other_key = Fernet.generate_key()
        other_token = Fernet(other_key).encrypt(b"secret").decode()

        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt(other_token)

    def test_decrypt_invalid_token_raises(self):
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt("not-a-valid-fernet-token")

    def test_decrypt_truncated_token_raises(self):
        token = encrypt("data")
        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt(token[:20])

    def test_returns_strings(self):
        ciphertext = encrypt("hello")
        assert isinstance(ciphertext, str)
        assert isinstance(decrypt(ciphertext), str)

    def test_special_characters(self):
        value = "p@$$w0rd!#%&*()"
        assert decrypt(encrypt(value)) == value

    def test_whitespace_preserved(self):
        value = "  leading and trailing  "
        assert decrypt(encrypt(value)) == value

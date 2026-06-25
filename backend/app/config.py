from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import EmailStr, model_validator

_ROOT = Path(__file__).parent.parent.parent  # readfine/

# Placeholder values shipped in .env.example. A production deploy that still
# uses these is trivially exploitable (forgeable sessions/JWTs, decryptable
# stored secrets), so the app refuses to boot with them unless debug is on.
_INSECURE_SECRET_KEYS = {"change-me", "changeme", ""}
_INSECURE_ENCRYPTION_KEYS = {"changemechangemechangemechangeme", "change-me", ""}
_MIN_SECRET_KEY_LEN = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[str(_ROOT / ".env"), ".env"],
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Database
    database_url: str

    # Security
    secret_key: str
    encryption_key: str
    allowed_hosts: list[str] = ["localhost", "127.0.0.1"]

    # Proxy / client IP resolution (rate limiting & login lockout)
    # 0 = no proxy in front: use the real TCP peer, ignore forwarding headers.
    # N = number of trusted reverse proxies in front; the client IP is read from
    #     X-Forwarded-For counting N entries from the right (never the leftmost,
    #     attacker-controlled entry).
    trusted_proxy_count: int = 0
    # Trust Cloudflare's CF-Connecting-IP header. Enable ONLY when the origin is
    # firewalled to Cloudflare IP ranges — otherwise the header is spoofable.
    trust_cloudflare: bool = False

    # App
    debug: bool = False
    app_name: str = "Readfine"

    # Initial admin (used only on first run)
    first_admin_email: EmailStr | None = None
    first_admin_password: str | None = None

    # Rate limiting
    rate_limit_login: str = "5/minute"
    rate_limit_register: str = "3/hour"
    rate_limit_reset_password: str = "2/hour"
    rate_limit_share_token: str = "20/minute"
    rate_limit_api_tokens: str = "5/hour"
    rate_limit_extract_readable: str = "10/minute"
    rate_limit_ai_summary: str = "15/minute"
    rate_limit_ai_context: str = "6/minute"
    rate_limit_ai_chat: str = "20/minute"
    rate_limit_ai_catchup: str = "1/minute"
    rate_limit_feedback: str = "3/hour"

    @model_validator(mode="after")
    def _reject_insecure_secrets(self) -> "Settings":
        """Fail fast in production if security keys are left at their defaults.

        Skipped when debug=True so local dev works out of the box with the
        placeholders from .env.example.
        """
        if self.debug:
            return self

        problems: list[str] = []
        if self.secret_key.strip().lower() in _INSECURE_SECRET_KEYS:
            problems.append("SECRET_KEY is set to a placeholder/default value")
        elif len(self.secret_key) < _MIN_SECRET_KEY_LEN:
            problems.append(
                f"SECRET_KEY is too short (min {_MIN_SECRET_KEY_LEN} chars)"
            )
        if self.encryption_key.strip().lower() in _INSECURE_ENCRYPTION_KEYS:
            problems.append("ENCRYPTION_KEY is set to a placeholder/default value")
        if self.first_admin_password and self.first_admin_password.strip().lower() in _INSECURE_SECRET_KEYS:
            problems.append("FIRST_ADMIN_PASSWORD is set to a placeholder/default value")

        if problems:
            raise ValueError(
                "Refusing to start with insecure configuration:\n  - "
                + "\n  - ".join(problems)
                + "\n\nGenerate fresh values (see .env.example) or set DEBUG=true for "
                "local development. Changing ENCRYPTION_KEY after data exists makes "
                "stored API keys and feed passwords undecryptable."
            )
        return self


settings = Settings()

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import EmailStr, field_validator, model_validator

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

    # Let users point the custom AI provider at a private address (Ollama on
    # localhost, an LLM container on the compose network). Off by default: the
    # base_url is user input, so on a hosted instance it would be a way to make
    # the server issue requests inside its own network. Turn this on when the
    # instance is yours and the model runs next to it. Feed URLs are unaffected.
    ai_allow_private_endpoints: bool = False

    # App
    debug: bool = False
    app_name: str = "Readfine"

    # Root log level (DEBUG/INFO/WARNING/ERROR). WARNING keeps the log to things
    # that need attention; INFO adds the running commentary from the scheduler and
    # fetcher. An unrecognised value falls back to WARNING.
    log_level: str = "WARNING"

    # Diagnostics: log one INFO line per outbound HTTP request (feed fetch, scrape,
    # readable extraction) with host, status, elapsed time and any rate-limit headers.
    # Off by default — it is verbose and only useful when investigating a specific
    # host's throttling. Enable via LOG_OUTBOUND_REQUESTS=true and restart the app.
    log_outbound_requests: bool = False

    # Hard cap on the body of any page we download (feed, scraped page, article).
    # Measured after decompression, so a small response that expands into gigabytes
    # is stopped too. 10 MB clears the largest real feeds and article pages by a wide
    # margin; raise it only if a genuine source turns out not to fit.
    max_fetch_bytes: int = 10 * 1024 * 1024

    # Video thumbnails are served through our own /img/video-thumb endpoint so a
    # reader's browser never contacts YouTube or Vimeo just by opening an article
    # (see video_thumb_service). The fetched images are kept in a disk cache under
    # this directory — one small JPEG per video — so a second reader of the same
    # video costs no upstream fetch. It is only a cache: deleting it loses nothing,
    # a missing entry is re-fetched on demand. Point it at a mounted volume in
    # Docker so it survives a restart.
    thumb_cache_dir: str = "/data/thumb-cache"
    # Hard ceiling on the whole cache. Thumbnails are tens to low hundreds of kB, so
    # 50 MB holds several hundred to a couple thousand distinct videos — a ceiling
    # most instances never reach, not an expected steady state. When it is passed,
    # the least-recently-used files are dropped until the cache fits again.
    thumb_cache_max_mb: int = 50
    # Soft sweep: a thumbnail not requested in this many days is dropped even below
    # the ceiling. This is what quietly clears entries for articles that have since
    # been purged — nobody opens them, so nothing refreshes their access time — with
    # no coupling to the articles table. Deliberately independent of retention: the
    # cache key is a video id shared across users, which has no single purge date.
    thumb_cache_idle_days: int = 30

    # Phase offset (minutes, 0–14) for the 15-min feed-fetch schedule. Shifts the
    # four fetch ticks off the default :00/:15/:30/:45 so a second instance sharing
    # the host (e.g. staging next to production) doesn't fetch at the same wall-clock
    # moment. 0 = default phase; other values normalise into 0–14 (mod 15).
    fetch_schedule_offset_min: int = 0

    # Initial admin (used only on first run)
    first_admin_email: EmailStr | None = None
    first_admin_password: str | None = None

    # How long a signed-in session survives without a visit. Sliding: every
    # response re-stamps it, so only a real absence runs it out. Two weeks was
    # short enough to log out someone who skipped a fortnight and meant to come
    # back; a month covers that with room without leaving a usable cookie lying
    # around for a season. Changing a password revokes every session regardless
    # (see session_token_version).
    session_max_age_days: int = 30

    # Rate limiting
    rate_limit_login: str = "5/minute"
    rate_limit_register: str = "3/hour"
    rate_limit_reset_password: str = "2/hour"
    rate_limit_share_token: str = "20/minute"
    rate_limit_api_tokens: str = "5/hour"
    rate_limit_extract_readable: str = "10/minute"
    # Saving a URL fetches an arbitrary user-supplied address, so it gets its own
    # budget rather than riding on the readable-extraction one.
    rate_limit_save_url: str = "10/minute"
    rate_limit_ai_summary: str = "15/minute"
    rate_limit_ai_context: str = "6/minute"
    rate_limit_ai_chat: str = "20/minute"
    rate_limit_ai_catchup: str = "1/minute"
    # Generating the interest profile builds its prompt from the whole reading history
    # and runs on the quality model, so it is the most expensive thing a single click
    # can start. The profile is meant to change every few weeks, hence an hourly cap.
    rate_limit_ai_preference: str = "5/hour"
    rate_limit_feedback: str = "3/hour"
    # Video-thumbnail proxy. Public (a shared article page renders video figures for
    # signed-out readers), so it is rate-limited by IP. A single article view fires
    # one request per video figure and the browser then caches it, so this is
    # generous on purpose — it exists to cap abuse, not normal reading.
    rate_limit_video_thumb: str = "120/minute"

    @field_validator("fetch_schedule_offset_min", mode="after")
    @classmethod
    def _normalise_fetch_offset(cls, v: int) -> int:
        """Fold the offset into the 0–14 range; the schedule repeats every 15 min."""
        return v % 15

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

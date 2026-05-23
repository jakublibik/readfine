from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import EmailStr

_ROOT = Path(__file__).parent.parent.parent  # readfine/


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

    # App
    debug: bool = False
    app_name: str = "Readfine"
    base_url: str = "http://localhost:8000"

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


settings = Settings()

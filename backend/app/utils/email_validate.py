"""Email format validation for web forms.

The API schemas use pydantic EmailStr, but web routes take bare `str` form
fields. Without validation an invalid address lands in the DB and is used
verbatim as a mail `To:` header — enabling SMTP header injection via newlines.
"""
from email_validator import EmailNotValidError, validate_email


def is_valid_email(raw: str) -> bool:
    """Return True if `raw` is a syntactically valid email address."""
    try:
        validate_email(raw, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def normalize_email(raw: str) -> str:
    """The form every address is stored and looked up in: trimmed, lowercased.

    Providers treat the local part case-insensitively in practice, and a match
    that depends on how someone typed their address locks them out of their own
    account. Migration 0118 brought the stored addresses in line.
    """
    return raw.strip().lower()

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

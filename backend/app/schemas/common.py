"""Limits shared by the schemas, taken from the columns the values land in.

Without them a value past the column (SmallInteger, String(100)) fails in the
database as a 500 instead of a validation error. The web forms build the same
schemas, so one set of rules covers both the API and the settings pages.
"""

SMALLINT_MAX = 32767
NAME_MAX = 100


def clean_name(v: str, what: str) -> str:
    """Strip a folder, label or filter name and check it fits its column."""
    v = v.strip()
    if not v:
        raise ValueError(f"{what} name cannot be empty")
    if len(v) > NAME_MAX:
        raise ValueError(f"{what} name cannot be longer than {NAME_MAX} characters")
    return v

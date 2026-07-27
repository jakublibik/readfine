"""Bot traps for public forms that trigger outbound email.

Two checks that need no configuration, no JavaScript and no third-party service,
so they work the same on a self-hosted box as they do behind a CDN:

* a honeypot field, hidden from sight and from assistive tech, that a human
  never fills in but a form-stuffing bot does;
* a signed timestamp proving the form was actually rendered by us and then
  submitted at human speed, not replayed straight at the endpoint.

Neither is bulletproof against an attacker who studies Readfine specifically.
They stop the common case: scripts that scrape a form, fill every field and
post it in bulk to use the instance as a mail relay.
"""

import time

from itsdangerous import BadSignature, Signer

from app.config import settings

# Named like a plausible optional field so a bot fills it in.
HONEYPOT_FIELD = "website"

# A person cannot render the page, type an email and two passwords, and submit
# in under this long. Autofill still needs the click.
MIN_FILL_SECONDS = 2.0

# How long a rendered form stays usable. Generous: people leave tabs open.
MAX_FORM_AGE_SECONDS = 6 * 3600

_signer = Signer(settings.secret_key, salt="readfine.form-guard")


def issue_form_ts(now: float | None = None) -> str:
    """Return a signed 'this form was rendered at' stamp for a public form."""
    stamp = int(now if now is not None else time.time())
    return _signer.sign(str(stamp)).decode()


def form_age_seconds(form_ts: str) -> float | None:
    """Age of a signed stamp in seconds, or None if it is missing or forged."""
    if not form_ts:
        return None
    try:
        raw = _signer.unsign(form_ts).decode()
    except BadSignature:
        return None
    try:
        return time.time() - int(raw)
    except ValueError:
        return None


def carry_form_ts(form_ts: str) -> str | None:
    """Return *form_ts* if it can be reused, or None when a fresh one is needed.

    Re-rendering the form after a validation error must not restart the clock.
    Someone who has already been on the page for a minute would otherwise be handed
    a brand-new stamp, and a password manager refilling both password fields in one
    click can then submit inside MIN_FILL_SECONDS and be treated as a bot.

    A stamp that is missing, forged or past MAX_FORM_AGE_SECONDS cannot be carried:
    reissuing it would leave the person stuck on "submit again" forever.
    """
    age = form_age_seconds(form_ts)
    if age is None or age > MAX_FORM_AGE_SECONDS:
        return None
    return form_ts


def check_form(honeypot: str, form_ts: str) -> str | None:
    """Screen a submitted public form.

    Returns None when the submission looks human, otherwise a short reason:

    ``"honeypot"``
        A bot, as good as certainly: the field is invisible and has nothing a
        person could react to. Callers should fake success so the script cannot
        tell the trap apart from a real signup.
    ``"too_fast"`` / ``"stale"``
        Suspicious, but only a heuristic, and both have an innocent reading. A
        person can beat MIN_FILL_SECONDS on a second attempt when a password
        manager refills the form in one click, and a stamp expires simply by
        leaving the tab open. Callers should show the form again with a retry
        message rather than swallowing the submission, which for a real person
        is indistinguishable from the account silently never being created.
    """
    if honeypot.strip():
        return "honeypot"

    age = form_age_seconds(form_ts)
    if age is None or age > MAX_FORM_AGE_SECONDS:
        return "stale"
    if age < MIN_FILL_SECONDS:
        return "too_fast"
    return None

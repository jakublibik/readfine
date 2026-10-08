"""SMTP helper: send emails using AppSettings configuration."""
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr

from app.models.settings import AppSettings
from app.utils.crypto import decrypt


def _get_password(s: AppSettings) -> str | None:
    if not s.smtp_password_encrypted:
        return None
    try:
        return decrypt(s.smtp_password_encrypted)
    except ValueError:
        return None


def send_email(
    s: AppSettings,
    to: str | list[str],
    subject: str,
    body: str,
    reply_to: str | None = None,
) -> None:
    """Send a plain-text email using the given AppSettings.

    ``to`` may be a single address or a list; a list is delivered in one SMTP
    transaction (all recipients share the To header).

    Raises:
        ValueError: if SMTP is not configured or no recipient is given
        smtplib.SMTPException: on connection/send errors
    """
    if not s.smtp_host or not s.smtp_from_email:
        raise ValueError("SMTP not configured (missing host or from address)")

    recipients = [to] if isinstance(to, str) else list(to)
    if not recipients:
        raise ValueError("No recipients")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = s.smtp_from_email
    msg["To"] = ", ".join(recipients)
    if reply_to:
        msg["Reply-To"] = reply_to

    _smtp_send(s, recipients, msg.as_string())


def send_html_email(
    s: AppSettings,
    to_list: list[str],
    subject: str,
    html_body: str,
    plain_body: str,
    bcc: list[str] | None = None,
) -> None:
    """Send a multipart HTML+plain email to one or more recipients.

    ``bcc`` addresses receive the message but are not exposed in any header, so
    recipients cannot see them (or each other, if only bcc is used).

    Raises:
        ValueError: if SMTP is not configured
        smtplib.SMTPException: on connection/send errors
    """
    if not s.smtp_host or not s.smtp_from_email:
        raise ValueError("SMTP not configured (missing host or from address)")

    bcc = bcc or []
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = s.smtp_from_email
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # Envelope carries both To and Bcc; the message headers only list To.
    _smtp_send(s, to_list + bcc, msg.as_string())


# The submission port for implicit TLS (RFC 8314): the connection is TLS from the
# first byte, with no STARTTLS. Sending a plaintext EHLO there just hangs until the
# timeout, so the port alone decides; there is no setting for it.
IMPLICIT_TLS_PORT = 465


def _connect(s: AppSettings, port: int) -> smtplib.SMTP:
    """An open, EHLO'd connection, encrypted the way the port and the TLS box ask:
    implicit TLS on 465, otherwise STARTTLS when "use TLS" is on, plain if off."""
    if port == IMPLICIT_TLS_PORT:
        return smtplib.SMTP_SSL(
            s.smtp_host, port, timeout=10, context=ssl.create_default_context(),
        )
    conn = smtplib.SMTP(s.smtp_host, port, timeout=10)
    try:
        conn.ehlo()
        if s.smtp_use_tls:
            conn.starttls(context=ssl.create_default_context())
    except BaseException:
        conn.close()
        raise
    return conn


def _smtp_send(s: AppSettings, recipients: list[str], raw_message: str) -> None:
    password = _get_password(s)
    port = s.smtp_port or 587

    # smtp_from_email may carry a display name ("Readfine <noreply@readfine.app>");
    # the envelope sender (MAIL FROM) must be the bare address only.
    envelope_from = parseaddr(s.smtp_from_email)[1] or s.smtp_from_email

    with _connect(s, port) as conn:
        if s.smtp_user and password:
            conn.login(s.smtp_user, password)
        conn.sendmail(envelope_from, recipients, raw_message)

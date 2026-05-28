"""SMTP helper: send emails using AppSettings configuration."""
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.models.settings import AppSettings
from app.utils.crypto import decrypt


def _get_password(s: AppSettings) -> str | None:
    if not s.smtp_password_encrypted:
        return None
    try:
        return decrypt(s.smtp_password_encrypted)
    except ValueError:
        return None


def send_email(s: AppSettings, to: str, subject: str, body: str) -> None:
    """Send a plain-text email using the given AppSettings.

    Raises:
        ValueError: if SMTP is not configured
        smtplib.SMTPException: on connection/send errors
    """
    if not s.smtp_host or not s.smtp_from_email:
        raise ValueError("SMTP not configured (missing host or from address)")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = s.smtp_from_email
    msg["To"] = to

    password = _get_password(s)
    port = s.smtp_port or 587

    _smtp_send(s, [to], msg.as_string())


def send_html_email(
    s: AppSettings,
    to_list: list[str],
    subject: str,
    html_body: str,
    plain_body: str,
) -> None:
    """Send a multipart HTML+plain email to one or more recipients.

    Raises:
        ValueError: if SMTP is not configured
        smtplib.SMTPException: on connection/send errors
    """
    if not s.smtp_host or not s.smtp_from_email:
        raise ValueError("SMTP not configured (missing host or from address)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = s.smtp_from_email
    msg["To"] = ", ".join(to_list)
    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    _smtp_send(s, to_list, msg.as_string())


def _smtp_send(s: AppSettings, recipients: list[str], raw_message: str) -> None:
    password = _get_password(s)
    port = s.smtp_port or 587

    if s.smtp_use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(s.smtp_host, port, timeout=10) as conn:
            conn.ehlo()
            conn.starttls(context=context)
            if s.smtp_user and password:
                conn.login(s.smtp_user, password)
            conn.sendmail(s.smtp_from_email, recipients, raw_message)
    else:
        with smtplib.SMTP(s.smtp_host, port, timeout=10) as conn:
            conn.ehlo()
            if s.smtp_user and password:
                conn.login(s.smtp_user, password)
            conn.sendmail(s.smtp_from_email, recipients, raw_message)

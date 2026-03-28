"""SMTP helper: send emails using AppSettings configuration."""
import smtplib
import ssl
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

    if s.smtp_use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(s.smtp_host, port, timeout=10) as conn:
            conn.ehlo()
            conn.starttls(context=context)
            if s.smtp_user and password:
                conn.login(s.smtp_user, password)
            conn.sendmail(s.smtp_from_email, [to], msg.as_string())
    else:
        with smtplib.SMTP(s.smtp_host, port, timeout=10) as conn:
            conn.ehlo()
            if s.smtp_user and password:
                conn.login(s.smtp_user, password)
            conn.sendmail(s.smtp_from_email, [to], msg.as_string())

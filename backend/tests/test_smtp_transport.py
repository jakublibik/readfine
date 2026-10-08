"""How utils.smtp connects: implicit TLS on 465, STARTTLS or plain elsewhere."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.utils.smtp import send_email


def _settings(port, use_tls=True):
    return SimpleNamespace(
        smtp_host="mail.example", smtp_port=port, smtp_use_tls=use_tls,
        smtp_from_email="Readfine <noreply@example.org>",
        smtp_user="u", smtp_password_encrypted=None,
    )


def _conn():
    conn = MagicMock()
    conn.__enter__.return_value = conn
    return conn


def test_port_465_uses_implicit_tls_whatever_the_box_says():
    conn = _conn()
    with patch("app.utils.smtp.smtplib.SMTP_SSL", return_value=conn) as ssl_cls, \
         patch("app.utils.smtp.smtplib.SMTP") as plain_cls:
        send_email(_settings(465, use_tls=False), "a@example.org", "s", "b")
    ssl_cls.assert_called_once()
    assert ssl_cls.call_args.kwargs["context"] is not None
    plain_cls.assert_not_called()
    conn.starttls.assert_not_called()
    conn.sendmail.assert_called_once_with("noreply@example.org", ["a@example.org"], conn.sendmail.call_args.args[2])


@pytest.mark.parametrize("use_tls", [True, False])
def test_other_ports_starttls_only_when_ticked(use_tls):
    conn = _conn()
    with patch("app.utils.smtp.smtplib.SMTP", return_value=conn) as plain_cls, \
         patch("app.utils.smtp.smtplib.SMTP_SSL") as ssl_cls:
        send_email(_settings(587, use_tls=use_tls), "a@example.org", "s", "b")
    plain_cls.assert_called_once()
    ssl_cls.assert_not_called()
    assert conn.starttls.called is use_tls
    conn.sendmail.assert_called_once()


def test_failed_starttls_closes_the_connection():
    conn = _conn()
    conn.starttls.side_effect = OSError("handshake")
    with patch("app.utils.smtp.smtplib.SMTP", return_value=conn):
        with pytest.raises(OSError):
            send_email(_settings(587), "a@example.org", "s", "b")
    conn.close.assert_called_once()

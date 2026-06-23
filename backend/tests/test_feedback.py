"""Tests for the in-app feedback / bug-report feature (POST /htmx/feedback).

Covers the security-relevant paths: admin-toggle gating, recipient selection
(all admins), Reply-To = sender, body contents, and input validation.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.conftest import make_scalar_result


def _settings(feedback_enabled=True, smtp=True):
    return SimpleNamespace(
        id=1,
        feedback_enabled=feedback_enabled,
        smtp_host="smtp.test" if smtp else None,
        smtp_from_email="Readfine <noreply@test>" if smtp else None,
    )


@pytest.fixture(autouse=True)
def _reset_limiter():
    from app.rate_limit import limiter
    limiter._storage.reset()
    yield


class TestFeedbackModalGet:
    def test_renders_when_available(self, client, mock_db):
        mock_db.execute.side_effect = [make_scalar_result(_settings())]
        resp = client.get("/htmx/feedback")
        assert resp.status_code == 200
        assert 'name="feedback_type"' in resp.text
        assert 'hx-post="/htmx/feedback"' in resp.text

    def test_403_when_disabled(self, client, mock_db):
        mock_db.execute.side_effect = [make_scalar_result(_settings(feedback_enabled=False))]
        resp = client.get("/htmx/feedback")
        assert resp.status_code == 403


class TestFeedbackSubmit:
    def test_success_emails_all_admins_with_reply_to(self, client, mock_db):
        mock_db.execute.side_effect = [
            make_scalar_result(_settings()),
            make_scalar_result(["admin1@test.com", "admin2@test.com"]),
        ]
        sent = []

        def capture(s, to, subject, body, reply_to=None):
            sent.append({"to": to, "subject": subject, "body": body, "reply_to": reply_to})

        with patch("app.utils.smtp.send_email", side_effect=capture):
            resp = client.post("/htmx/feedback", data={
                "feedback_type": "bug",
                "subject": "Broken button",
                "message": "The star icon does nothing.",
            })

        assert resp.status_code == 200
        # On success the form is replaced by a confirmation, not kept filled.
        assert "has been sent" in resp.text
        assert "<textarea" not in resp.text
        assert len(sent) == 2
        assert {m["to"] for m in sent} == {"admin1@test.com", "admin2@test.com"}
        for m in sent:
            assert m["reply_to"] == "user1@test.com"          # sender's account email
            assert m["subject"] == "[Readfine bug] Broken button"
            assert "user1@test.com" in m["body"]              # identity always in body
            assert "The star icon does nothing." in m["body"]

    def test_unknown_type_falls_back_to_feedback(self, client, mock_db):
        mock_db.execute.side_effect = [
            make_scalar_result(_settings()),
            make_scalar_result(["admin@test.com"]),
        ]
        sent = []
        with patch("app.utils.smtp.send_email",
                   side_effect=lambda *a, **k: sent.append(k.get("subject"))):
            resp = client.post("/htmx/feedback", data={
                "feedback_type": "haxxor", "subject": "Hi", "message": "msg",
            })
        assert resp.status_code == 200
        assert sent == ["[Readfine feedback] Hi"]

    def test_disabled_toggle_blocks_send(self, client, mock_db):
        mock_db.execute.side_effect = [make_scalar_result(_settings(feedback_enabled=False))]
        with patch("app.utils.smtp.send_email") as send:
            resp = client.post("/htmx/feedback", data={
                "subject": "Hi", "message": "msg",
            })
        assert resp.status_code == 403
        send.assert_not_called()

    def test_no_smtp_blocks_send(self, client, mock_db):
        mock_db.execute.side_effect = [make_scalar_result(_settings(smtp=False))]
        with patch("app.utils.smtp.send_email") as send:
            resp = client.post("/htmx/feedback", data={
                "subject": "Hi", "message": "msg",
            })
        assert resp.status_code == 403
        send.assert_not_called()

    def test_empty_fields_rejected(self, client, mock_db):
        mock_db.execute.side_effect = [make_scalar_result(_settings())]
        with patch("app.utils.smtp.send_email") as send:
            resp = client.post("/htmx/feedback", data={
                "subject": "  ", "message": "",
            })
        assert resp.status_code == 400
        send.assert_not_called()
        # Error re-renders the form (so the user can correct and retry).
        assert "<textarea" in resp.text

    def test_validation_error_preserves_input(self, client, mock_db):
        # Missing message, but subject should survive in the re-rendered form.
        mock_db.execute.side_effect = [make_scalar_result(_settings())]
        with patch("app.utils.smtp.send_email"):
            resp = client.post("/htmx/feedback", data={
                "feedback_type": "bug", "subject": "Keep me", "message": "",
            })
        assert resp.status_code == 400
        assert 'value="Keep me"' in resp.text

    def test_overlong_subject_rejected(self, client, mock_db):
        mock_db.execute.side_effect = [make_scalar_result(_settings())]
        with patch("app.utils.smtp.send_email") as send:
            resp = client.post("/htmx/feedback", data={
                "subject": "x" * 201, "message": "msg",
            })
        assert resp.status_code == 400
        send.assert_not_called()

    def test_no_admins_returns_error(self, client, mock_db):
        mock_db.execute.side_effect = [
            make_scalar_result(_settings()),
            make_scalar_result([]),
        ]
        with patch("app.utils.smtp.send_email") as send:
            resp = client.post("/htmx/feedback", data={
                "subject": "Hi", "message": "msg",
            })
        assert resp.status_code == 503
        send.assert_not_called()

    def test_smtp_failure_reported(self, client, mock_db):
        import smtplib
        mock_db.execute.side_effect = [
            make_scalar_result(_settings()),
            make_scalar_result(["admin@test.com"]),
        ]
        with patch("app.utils.smtp.send_email",
                   side_effect=smtplib.SMTPException("boom")):
            resp = client.post("/htmx/feedback", data={
                "subject": "Hi", "message": "msg",
            })
        assert resp.status_code == 502

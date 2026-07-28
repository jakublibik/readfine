"""User feedback / bug report modal."""
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.user import User
from app.rate_limit import limiter
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-app"])

_FEEDBACK_TYPES = {"bug", "feedback", "other"}
_FEEDBACK_SUBJECT_MAX = 200
_FEEDBACK_MESSAGE_MAX = 5000


async def _feedback_settings(db: AsyncSession):
    """Return (AppSettings|None, smtp_available, enabled) for the feedback feature."""
    from app.models.settings import AppSettings as _AS

    s = (await db.execute(select(_AS).where(_AS.id == 1))).scalar_one_or_none()
    smtp_available = bool(s and s.smtp_host and s.smtp_from_email)
    enabled = bool(s and s.feedback_enabled)
    return s, smtp_available, enabled


@router.get("/htmx/feedback", response_class=HTMLResponse)
async def htmx_feedback_modal_get(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, smtp_available, enabled = await _feedback_settings(db)
    if not (enabled and smtp_available):
        return HTMLResponse("Not available", status_code=403)
    return templates.TemplateResponse(request, "app/partials/feedback_modal.html", {})


@router.post("/htmx/feedback", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_feedback)
async def htmx_feedback_submit(
    request: Request,
    feedback_type: str = Form("feedback"),
    subject: str = Form(""),
    message: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime, timezone
    from app.utils.smtp import send_email

    ftype = (feedback_type or "").strip().lower()
    if ftype not in _FEEDBACK_TYPES:
        ftype = "feedback"
    # Collapse whitespace/newlines: subject becomes a single header line, so a
    # multi-line value would otherwise be rejected at send time (header folding).
    subject = " ".join((subject or "").split())
    message = (message or "").strip()

    def _form(error: str, status_code: int) -> HTMLResponse:
        """Re-render the form with the submitted values preserved + an error."""
        return templates.TemplateResponse(
            request, "app/partials/feedback_modal.html",
            {"error": error, "values": {"feedback_type": ftype, "subject": subject, "message": message}},
            status_code=status_code,
        )

    s, smtp_available, enabled = await _feedback_settings(db)
    if not (enabled and smtp_available):
        return _form("Feedback is not available.", status_code=403)

    if not subject or not message:
        return _form("Please fill in both a subject and a message.", status_code=400)
    if len(subject) > _FEEDBACK_SUBJECT_MAX or len(message) > _FEEDBACK_MESSAGE_MAX:
        return _form("Your subject or message is too long.", status_code=400)

    admin_emails = (await db.execute(
        select(User.email).where(User.role == "admin", User.email_verified == True)  # noqa: E712
    )).scalars().all()
    if not admin_emails:
        return _form("No administrator is available to receive feedback.", status_code=503)

    mail_subject = f"[Readfine {ftype}] {subject}"
    sent_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f"Type: {ftype}\n"
        f"From: {user.email} (user id {user.id})\n"
        f"Sent: {sent_at}\n"
        f"\n{message}\n"
    )

    try:
        # One SMTP transaction to all admins: avoids per-admin latency and the
        # partial-send case where some admins get the message and others don't.
        send_email(s, to=admin_emails, subject=mail_subject, body=body, reply_to=user.email)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to send feedback email: %s", e)
        return _form("Sorry, we couldn't send your message. Please try again later.", status_code=502)

    return templates.TemplateResponse(request, "app/partials/feedback_sent.html", {})

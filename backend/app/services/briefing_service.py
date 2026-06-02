"""Briefing service: scheduled email digest per UserCatchupConfig."""
from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import css_inline
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.settings import AppSettings
from app.models.user import CatchupLog, UserCatchupConfig, User
from app.services.catchup_service import (
    apply_catchup_limit,
    build_articles_meta,
    fetch_catchup_articles,
)
from app.templating import templates
from app.utils.markdown import md_render
from app.utils.smtp import send_html_email

_inliner = css_inline.CSSInliner(keep_style_tags=True)

_PERIOD_LABELS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "7days": "Last 7 days",
}

_DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def compute_next_send_at(
    interval: str,
    day: int | None,
    time_str: str,
    tz_str: str | None,
) -> datetime:
    """Return the next UTC datetime this briefing should be sent.

    Always returns a future datetime — never the current moment or past.
    interval: 'daily' | 'weekly'
    day: 0=Mon … 6=Sun (only for weekly)
    time_str: 'HH:MM' in user timezone
    tz_str: IANA timezone string, defaults to UTC
    """
    try:
        tz = ZoneInfo(tz_str or "UTC")
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")

    try:
        h, m = int(time_str[:2]), int(time_str[3:5])
    except (ValueError, IndexError):
        h, m = 8, 0

    now = datetime.now(tz)

    if interval == "daily":
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)

    # weekly
    target_day = day if day is not None else 0
    days_ahead = (target_day - now.weekday()) % 7
    candidate = (now + timedelta(days=days_ahead)).replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


_BQ_OPEN = '<div style="border-left:3px solid #e4e4e7;margin:0 0 12px 0;padding:4px 0 4px 16px;color:#71717a;">'
_BQ_CLOSE = "</div>"


def _build_email_html(
    markdown_text: str,
    subject: str,
    config_name: str,
    period_label: str,
    date_label: str,
    article_count: int,
) -> str:
    content_html = md_render(markdown_text)
    # Outlook renders <blockquote> with its own grey border regardless of CSS — replace with <div>
    content_html = content_html.replace("<blockquote>", _BQ_OPEN).replace("</blockquote>", _BQ_CLOSE)
    raw = templates.env.get_template("email/briefing.html").render(
        subject=subject,
        config_name=config_name,
        period_label=period_label,
        date_label=date_label,
        content=content_html,
        article_count=article_count,
    )
    return _inliner.inline(raw)


def _compose_subject(config_name: str, period: str, now_utc: datetime) -> str:
    period_label = _PERIOD_LABELS.get(period, period)
    date_label = f"{now_utc:%b} {now_utc.day}, {now_utc.year}"
    return f"{config_name} — {period_label} · {date_label}"


async def send_briefing(
    config: UserCatchupConfig,
    user: User,
    db: AsyncSession,
    app_settings: AppSettings,
    test_mode: bool = False,
) -> None:
    """Execute the full briefing pipeline: fetch → AI → email → log.

    On scope error (deleted feed/folder): sets briefing_enabled=False permanently.
    On AI error (transient): raises — caller handles retry logic.
    On SMTP error: raises smtplib.SMTPException — caller disables briefing.
    """
    if not app_settings.smtp_host or not app_settings.smtp_from_email:
        if not test_mode:
            config.briefing_enabled = False
            config.briefing_last_error = "SMTP not configured — briefing disabled."
            config.briefing_next_send_at = None
            await db.commit()
        return

    tz_str = (user.settings.timezone if user.settings else None) or "UTC"

    try:
        articles = await fetch_catchup_articles(
            user_id=user.id,
            db=db,
            period=config.period,
            scope_include=config.scope_include,
            filter_status=config.filter_status,
            filter_labeled=config.filter_labeled,
            filter_score_min=config.filter_score_min,
            tz_str=tz_str,
        )
    except ValueError as exc:
        # Permanent error: scope items no longer exist
        config.briefing_enabled = False
        config.briefing_last_error = f"Scope error: {exc}"
        config.briefing_next_send_at = None
        await db.commit()
        return

    now_utc = datetime.now(timezone.utc)

    if not articles:
        # Silent skip: log article_count=0, schedule next send
        db.add(CatchupLog(
            user_id=user.id,
            config_id=config.id,
            article_count=0,
            input_tokens=0,
            output_tokens=0,
            model=None,
            provider=None,
            model_slot=config.model_slot,
        ))
        config.briefing_next_send_at = compute_next_send_at(
            config.briefing_interval, config.briefing_day, config.briefing_time or "08:00", tz_str
        )
        await db.commit()
        return

    from app.services.ai_service import catch_me_up, get_ai_client  # noqa: PLC0415

    ai_client_info = await get_ai_client(user.id, config.model_slot, db)
    if ai_client_info is None:
        raise RuntimeError("No AI client available — check API keys in Settings → AI")

    client, provider, model = ai_client_info

    scoring_available = bool(
        user.settings and user.settings.ai_scoring_enabled_default
    ) if user.settings else False

    sampled = apply_catchup_limit(articles, config.article_limit, scoring_available)
    articles_meta = build_articles_meta(sampled, include_snippet=config.include_snippet)

    text, input_tokens, output_tokens = await catch_me_up(
        articles_meta=articles_meta,
        period=config.period,
        client=client,
        provider=provider,
        model=model,
        custom_prompt=config.custom_prompt or None,
    )

    subject = _compose_subject(config.name, config.period, now_utc)
    if test_mode:
        subject = f"[TEST] {subject}"

    period_label = _PERIOD_LABELS.get(config.period, config.period)
    date_label = f"{now_utc:%b} {now_utc.day}, {now_utc.year}"
    html_body = _build_email_html(text, subject, config.name, period_label, date_label, len(sampled))

    extra_recipients: list[str] = []
    if config.briefing_recipients:
        try:
            extra_recipients = json.loads(config.briefing_recipients) or []
        except (json.JSONDecodeError, TypeError):
            extra_recipients = []

    to_list = [user.email] + extra_recipients

    # May raise smtplib.SMTPException — caller handles
    send_html_email(app_settings, to_list, subject, html_body, text)

    db.add(CatchupLog(
        user_id=user.id,
        config_id=config.id,
        article_count=len(sampled),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model,
        provider=provider,
        model_slot=config.model_slot,
    ))

    if not test_mode:
        config.briefing_last_sent_at = now_utc
        config.briefing_last_error = None
        config.briefing_retry_count = 0
        config.briefing_next_send_at = compute_next_send_at(
            config.briefing_interval, config.briefing_day, config.briefing_time or "08:00", tz_str
        )

    await db.commit()

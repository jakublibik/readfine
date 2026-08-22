"""On-demand AI over a single article (summary, context) and the chat panels
(per-article and general)."""
import html as html_module
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.article import Article, ArticleAiChat, ArticleAiJob, UserArticleState
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.services.ai_jobs import ai_enabled_globally, normalize_content
from app.services.article import add_article_access_joins, article_access_predicate
from app.templating import templates

router = APIRouter(tags=["web-app"])


async def _get_article_access(user: User, article_id: int, db: AsyncSession):
    """Return Article ORM object if user has access, else None."""
    stmt = add_article_access_joins(select(Article), user.id).where(
        Article.id == article_id, article_access_predicate()
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _ai_macros():
    """Macro module for the AI result blocks — the same source article_detail.html
    renders from, so a generated block matches how a stored one is drawn."""
    return templates.env.get_template("app/partials/ai_blocks.html").module


def _ai_summary_block(article_id: int, summary: str, truncated: bool = False) -> str:
    return str(_ai_macros().ai_summary(article_id, summary, truncated))


def _ai_context_block(article_id: int, context: str) -> str:
    return str(_ai_macros().ai_context(article_id, context))


_CHAT_MAX_MESSAGES = 10  # 5 user + 5 assistant turns


def _chat_macros():
    """Macro module for the chat panel — the same source article_detail.html and
    main.html draw the panel from, so sending a message can't reshape it."""
    return templates.env.get_template("app/partials/chat.html").module


def _render_chat_area(article_id: int, messages: list[dict],
                      include_article: bool = True,
                      error: str = "",
                      article_title: str = "") -> str:
    return str(_chat_macros().chat_area(
        article_id, messages, include_article, error, article_title
    ))


def _render_general_chat_area(messages: list[dict], error: str = "") -> str:
    return str(_chat_macros().general_chat_area(messages, error))


def _ai_spinner(target_id: str, poll_url: str) -> str:
    return str(_ai_macros().ai_spinner(target_id, poll_url, "Generating summary…"))


async def _require_quality_ai_for_article(
    user: User, article_id: int, db: AsyncSession, *, target_id: str, too_short_label: str
):
    """Shared guard for on-demand AI (summary / context): AI enabled globally, a
    quality model configured, the article accessible, and its content long enough.

    Returns an error ``HTMLResponse`` (rendered into ``target_id``) on any failed
    check, or ``(article, settings, content_text)`` on success.
    """
    from app.services.ai_summary_service import _MIN_CONTENT_CHARS

    def _note(text: str) -> HTMLResponse:
        return HTMLResponse(f'<div id="{target_id}" class="text-xs text-gray-400 py-1">{text}</div>')

    if not await ai_enabled_globally(db):
        return _note("AI is disabled.")

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return _note("Main AI model not configured.")

    article = await _get_article_access(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)

    content_text = normalize_content(
        article.title, article.readable_content or article.content, settings.ai_content_limit
    )
    if len(content_text) < _MIN_CONTENT_CHARS:
        return _note(f"Article is too short for {too_short_label} (minimum {_MIN_CONTENT_CHARS} characters).")

    return article, settings, content_text


@router.post("/htmx/articles/{article_id}/ai-summary", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_summary)
async def htmx_ai_summary_trigger(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """On-demand: run summary synchronously and return result block."""
    guard = await _require_quality_ai_for_article(
        user, article_id, db, target_id=f"ai-summary-{article_id}", too_short_label="a summary"
    )
    if isinstance(guard, HTMLResponse):
        return guard
    article, settings, content_text = guard

    from app.services.ai_summary_service import run_summary_on_demand
    summary, truncated, error = await run_summary_on_demand(article, user.id, db)
    if summary is None:
        msg = html_module.escape(error) if error else "Summary unavailable."
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-red-500 py-1">Summary failed: {msg}</div>'
        )
    return HTMLResponse(_ai_summary_block(article_id, summary, truncated))


@router.get("/htmx/articles/{article_id}/ai-summary/poll", response_class=HTMLResponse)
async def htmx_ai_summary_poll(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Poll summary job status and return final block or keep spinner."""
    job = await db.scalar(
        select(ArticleAiJob).where(
            ArticleAiJob.article_id == article_id,
            ArticleAiJob.user_id == user.id,
            ArticleAiJob.operation == "summary",
        )
    )

    if job is None:
        # No job (never queued, or cancelled by unstarring) — stop polling instead
        # of spinning forever on a summary that is not coming.
        return HTMLResponse(f'<div id="ai-summary-{article_id}"></div>')

    if job.status == "pending":
        return HTMLResponse(_ai_spinner(f"ai-summary-{article_id}", f"/htmx/articles/{article_id}/ai-summary/poll"))

    if job.status == "failed":
        msg = html_module.escape((job.error_message or "Unknown error")[:120])
        return HTMLResponse(
            f'<div id="ai-summary-{article_id}" class="text-xs text-red-500 py-1">Summary failed: {msg}</div>'
        )

    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article_id,
        )
    )
    if state and state.ai_summary:
        return HTMLResponse(
            _ai_summary_block(article_id, state.ai_summary, state.ai_summary_truncated)
        )

    return HTMLResponse(f'<div id="ai-summary-{article_id}"></div>')


@router.post("/htmx/articles/{article_id}/ai-context", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_context)
async def htmx_ai_context_trigger(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """On-demand: call AI directly and return context block (synchronous, may take several seconds)."""
    guard = await _require_quality_ai_for_article(
        user, article_id, db, target_id=f"ai-context-{article_id}", too_short_label="context generation"
    )
    if isinstance(guard, HTMLResponse):
        return guard
    article, settings, content_text = guard

    form = await request.form()
    focus = (form.get("focus") or "").strip() or None

    from app.services.ai_service import get_ai_client, get_article_context
    client, provider, model = await get_ai_client(user.id, "quality", db)
    if client is None:
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-gray-400 py-1">Main AI model not configured.</div>'
        )

    try:
        result, in_tok, out_tok = await get_article_context(
            content_text, client, provider, model,
            base_prompt=settings.ai_context_prompt,
            focus=focus,
        )
    except Exception as exc:
        msg = html_module.escape(str(exc)[:120])
        return HTMLResponse(
            f'<div id="ai-context-{article_id}" class="text-xs text-red-500 py-1">Context failed: {msg}</div>'
        )

    now = datetime.now(timezone.utc)
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article_id,
        )
    )
    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)
    state.ai_context = result

    from sqlalchemy.dialects.postgresql import insert as pg_insert
    await db.execute(
        pg_insert(ArticleAiJob).values(
            article_id=article_id,
            user_id=user.id,
            operation="context",
            status="success",
            provider=provider,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            processed_at=now,
        ).on_conflict_do_update(
            index_elements=["article_id", "user_id", "operation"],
            set_={
                "status": "success",
                "provider": provider,
                "model": model,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "processed_at": now,
            },
        )
    )
    await db.commit()

    return HTMLResponse(_ai_context_block(article_id, result))


def _ai_chat_error_message(exc: Exception) -> str:
    """Map an AI-provider exception to a user-facing chat error line."""
    exc_str = str(exc)
    status = getattr(exc, "status_code", None)
    if status == 529 or "529" in exc_str or "overloaded" in exc_str.lower():
        return "AI provider is overloaded. Please try again in a moment."
    if status == 429 or "429" in exc_str or "rate_limit" in exc_str.lower():
        return "Rate limit reached. Please wait a moment and try again."
    if status and status >= 500:
        return "AI provider returned a server error. Please try again."
    return "Chat failed. Please try again."


@router.post("/htmx/ai-chat", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_chat)
async def htmx_general_ai_chat(
    request: Request,
    message: str = Form(...),
    include_article: str = Form(""),
    history: str = Form("[]"),
    article_id: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ai_on = await ai_enabled_globally(db)
    if not ai_on:
        return HTMLResponse(
            f'<div id="general-chat-area" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">AI is disabled.</p></div>'
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return HTMLResponse(
            f'<div id="general-chat-area" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">Main AI model not configured.</p></div>'
        )
    if not getattr(settings, 'ai_chat_enabled', False):
        return HTMLResponse("", status_code=403)

    msg_text = message.strip()[:2000]
    if not msg_text:
        return HTMLResponse("", status_code=400)

    try:
        current_messages: list[dict] = json.loads(history)
        if not isinstance(current_messages, list):
            current_messages = []
    except (json.JSONDecodeError, ValueError):
        current_messages = []

    current_messages.append({"role": "user", "content": msg_text})
    if len(current_messages) > _CHAT_MAX_MESSAGES:
        current_messages = current_messages[-_CHAT_MAX_MESSAGES:]

    tier = "quality"
    use_article = (include_article == "on")

    article_ctx = None
    art_id: int | None = None
    article = None
    if use_article and article_id.strip().isdigit():
        art_id = int(article_id)
        article = await _get_article_access(user, art_id, db)
        if article:
            article_ctx = normalize_content(
                article.title,
                article.readable_content or article.content,
                settings.ai_content_limit,
            )

    from app.services.ai_service import get_ai_client, chat_with_article
    client, provider, model = await get_ai_client(user.id, tier, db)
    if client is None:
        return HTMLResponse(
            _render_general_chat_area(
                current_messages[:-1],
                error="Main AI model not configured.",
            )
        )

    try:
        response_text, in_tok, out_tok = await chat_with_article(current_messages, article_ctx, client, provider, model)
    except Exception as exc:
        return HTMLResponse(
            _render_general_chat_area(current_messages[:-1], error=_ai_chat_error_message(exc))
        )

    current_messages.append({"role": "assistant", "content": response_text})
    if len(current_messages) > _CHAT_MAX_MESSAGES:
        current_messages = current_messages[-_CHAT_MAX_MESSAGES:]

    if use_article and art_id and article:
        chat_record = await db.scalar(
            select(ArticleAiChat).where(
                ArticleAiChat.user_id == user.id,
                ArticleAiChat.article_id == art_id,
            )
        )
        if chat_record is None:
            chat_record = ArticleAiChat(user_id=user.id, article_id=art_id, messages=[])
            db.add(chat_record)
        saved = list(chat_record.messages or [])
        saved.append({"role": "user", "content": msg_text})
        saved.append({"role": "assistant", "content": response_text})
        if len(saved) > _CHAT_MAX_MESSAGES:
            saved = saved[-_CHAT_MAX_MESSAGES:]
        chat_record.messages = saved
        chat_record.total_input_tokens = (chat_record.total_input_tokens or 0) + in_tok
        chat_record.total_output_tokens = (chat_record.total_output_tokens or 0) + out_tok
        chat_record.updated_at = datetime.now(timezone.utc)
    else:
        from app.models.article import GeneralChatLog
        db.add(GeneralChatLog(user_id=user.id, input_tokens=in_tok, output_tokens=out_tok))
    await db.commit()

    return HTMLResponse(_render_general_chat_area(current_messages))


@router.delete("/htmx/ai-chat", response_class=HTMLResponse)
async def htmx_general_ai_chat_clear(
    user: User = Depends(get_current_user),
):
    return HTMLResponse(_render_general_chat_area([]))


@router.post("/htmx/articles/{article_id}/ai-chat", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_ai_chat)
async def htmx_ai_chat(
    article_id: int,
    request: Request,
    message: str = Form(...),
    include_article: str = Form(""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    ai_on = await ai_enabled_globally(db)
    if not ai_on:
        return HTMLResponse(
            f'<div id="chat-area-{article_id}" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">AI is disabled.</p></div>'
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if not settings or not settings.ai_quality_provider or not settings.ai_quality_model:
        return HTMLResponse(
            f'<div id="chat-area-{article_id}" '
            f'class="flex-1 overflow-hidden flex flex-col px-2 sm:px-4 py-3">'
            f'<p class="text-xs text-gray-400 py-2">Main AI model not configured.</p></div>'
        )
    if not getattr(settings, 'ai_chat_enabled', False):
        return HTMLResponse("", status_code=403)

    msg_text = message.strip()
    if not msg_text:
        return HTMLResponse("", status_code=400)

    article = await _get_article_access(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)

    chat = await db.scalar(
        select(ArticleAiChat).where(
            ArticleAiChat.user_id == user.id,
            ArticleAiChat.article_id == article_id,
        )
    )
    if chat is None:
        chat = ArticleAiChat(user_id=user.id, article_id=article_id, messages=[])
        db.add(chat)

    current_messages: list[dict] = list(chat.messages or [])
    current_messages.append({"role": "user", "content": msg_text})

    tier = "quality"
    use_article = (include_article == "on")

    article_ctx = None
    if use_article:
        article_ctx = normalize_content(article.title, article.readable_content or article.content, settings.ai_content_limit)

    from app.services.ai_service import get_ai_client, chat_with_article
    client, provider, model = await get_ai_client(user.id, tier, db)
    title = article.title or ""
    if client is None:
        return HTMLResponse(_render_chat_area(
            article_id, current_messages[:-1], use_article,
            error="Main AI model not configured.",
            article_title=title,
        ))

    try:
        response_text, in_tok, out_tok = await chat_with_article(current_messages, article_ctx, client, provider, model)
    except Exception as exc:
        return HTMLResponse(_render_chat_area(
            article_id, current_messages[:-1], use_article,
            error=_ai_chat_error_message(exc), article_title=title,
        ))

    current_messages.append({"role": "assistant", "content": response_text})
    if len(current_messages) > _CHAT_MAX_MESSAGES:
        current_messages = current_messages[-_CHAT_MAX_MESSAGES:]
    chat.messages = current_messages  # reassign — SQLAlchemy JSONB change tracking
    chat.total_input_tokens = (chat.total_input_tokens or 0) + in_tok
    chat.total_output_tokens = (chat.total_output_tokens or 0) + out_tok
    chat.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return HTMLResponse(_render_chat_area(article_id, current_messages, use_article,
                                          article_title=title))


@router.delete("/htmx/articles/{article_id}/ai-chat", response_class=HTMLResponse)
async def htmx_ai_chat_clear(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    chat = await db.scalar(
        select(ArticleAiChat).where(
            ArticleAiChat.user_id == user.id,
            ArticleAiChat.article_id == article_id,
        )
    )
    if chat is not None:
        chat.messages = []
        chat.updated_at = datetime.now(timezone.utc)
        await db.commit()
    return HTMLResponse(_render_chat_area(article_id, []))

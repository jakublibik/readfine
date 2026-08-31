"""Article list, detail and per-article state actions (read / star / archive /
labels / share / readable extraction)."""
import asyncio
import json
import logging
import secrets
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import delete as sa_delete, func, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.config import settings as app_settings_config
from app.database import get_db
from app.models.article import Article, ArticleAiChat, ArticleAiJob, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel
from app.models.user import User, UserSettings
from app.rate_limit import limiter
from app.schemas.article import ArticleStateUpdate
from app.services.article import (
    add_article_access_joins, article_access_predicate,
    filter_accessible_article_ids, get_article, list_articles,
    mark_articles_read_batch, toggle_article_state, update_article_state,
)
from app.services.label_service import list_labels
from app.services.readable_service import apply_readable_result
from app.templating import templates

from .common import _ai_availability, _badge_html

logger = logging.getLogger(__name__)

router = APIRouter(tags=["web-app"])


async def _extract_readable_bg(
    article_id: int,
    url: str,
    auth_user: str | None,
    auth_pass_enc: str | None,
) -> None:
    """Background readable extraction fired when user opens an article."""
    from app.database import async_session_factory
    from app.services.readable_service import extract_readable
    from app.utils.crypto import feed_auth

    auth_user, auth_pass = feed_auth(
        auth_user, auth_pass_enc, context=f"article {article_id}"
    ) or (None, None)

    loop = asyncio.get_running_loop()
    try:
        content, error, http_status, published_at = await loop.run_in_executor(
            None, extract_readable, url, auth_user, auth_pass
        )
    except Exception as exc:
        content, error, http_status, published_at = None, str(exc)[:200], None, None
        logger.warning("readable bg: extraction error for article %d: %s", article_id, exc)

    async with async_session_factory() as db:
        article = (await db.execute(
            select(Article).where(Article.id == article_id)
        )).scalar_one_or_none()
        if not article:
            return
        apply_readable_result(article, content, error, http_status, published_at)
        # Mirror the batch readable path: once readable finishes, complete any
        # label-deferred AI pipeline (scoring/filters/summary). run_pipeline_for_
        # article_all_users is label-scoped — it only runs for users who labeled
        # this article — so opening an unlabeled article never triggers scoring.
        from app.services.ai_pipeline_service import run_pipeline_for_article_all_users
        if content:
            await run_pipeline_for_article_all_users(article, db)
        elif article.readable_status == "failed":
            # Terminal failure — score with the RSS content we already have.
            await run_pipeline_for_article_all_users(article, db)
        await db.commit()
        logger.info("readable bg: article %d → %s", article_id, article.readable_status)


# Grace period after starring before the summary is processed immediately, so a
# quick unstar (mis-click) cancels it instead of spending tokens. The 5-minute
# batch worker still backstops anything left pending.
_STAR_SUMMARY_DEBOUNCE_S = 5.0


async def _summary_after_star_bg(article_id: int, user_id: int) -> None:
    """Wait out the debounce, then process the pending summary job immediately
    (instead of waiting for the batch) — unless the star was removed meanwhile."""
    await asyncio.sleep(_STAR_SUMMARY_DEBOUNCE_S)
    from app.database import async_session_factory
    from app.services.ai_pipeline_service import _run_summary_now

    async with async_session_factory() as db:
        state = await db.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id == article_id,
            )
        )
        # Unstarred during the debounce, or summary already produced → skip.
        if state is None or not state.is_starred or state.ai_summary:
            return
        article = await db.scalar(select(Article).where(Article.id == article_id))
        if article is None:
            return
        await _run_summary_now(article, user_id, db)
        await db.commit()
        logger.info("star summary: article=%d user=%d processed", article_id, user_id)


@router.post("/htmx/articles/set-read-batch")
async def htmx_set_read_batch(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    data = await request.json()
    ids = [int(i) for i in (data.get("ids") or [])[:500] if str(i).isdigit()]
    await mark_articles_read_batch(user, ids, db)
    return HTMLResponse("", status_code=200)


async def _label_badge_oob(user_id: int, label_id: int | None, labeled_only: bool, db: AsyncSession) -> str:
    """Return OOB HTML snippets to update label badge(s) in the sidebar."""
    if not label_id and not labeled_only:
        return ""
    oob = ""
    if label_id:
        lu = (await db.scalar(
            select(func.count(ArticleLabel.article_id))
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user_id))
            .where(
                ArticleLabel.user_id == user_id,
                ArticleLabel.label_id == label_id,
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
        )) or 0
        lt = (await db.scalar(
            select(func.count(ArticleLabel.article_id))
            .where(ArticleLabel.user_id == user_id, ArticleLabel.label_id == label_id)
        )) or 0
        oob += f'<span id="label-badge-{label_id}" hx-swap-oob="innerHTML">{_badge_html(lu, lt)}</span>'
    # Aggregate "Labels" badge
    all_unread = (await db.scalar(
        select(func.count(Article.id.distinct()))
        .select_from(Article)
        .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user_id))
        .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id))
        .where((UserArticleState.is_read == None) | (UserArticleState.is_read == False))
    )) or 0
    all_total = (await db.scalar(
        select(func.count()).select_from(
            select(ArticleLabel.article_id).where(ArticleLabel.user_id == user_id).distinct().subquery()
        )
    )) or 0
    oob += f'<span id="label-badge-all" hx-swap-oob="innerHTML">{_badge_html(all_unread, all_total)}</span>'
    return oob


def _is_mobile(request: Request) -> bool:
    """Coarse mobile detection from the User-Agent, used only to pick the mobile
    vs web list density (a wrong guess just flips density, never breaks anything)."""
    ua = request.headers.get("user-agent", "").lower()
    return any(x in ua for x in ("mobile", "android", "iphone", "ipad"))


async def _get_chat_article_ids(user_id: int, article_ids: list[int], db: AsyncSession) -> set[int]:
    if not article_ids:
        return set()
    rows = await db.execute(
        select(ArticleAiChat.article_id).where(
            ArticleAiChat.user_id == user_id,
            ArticleAiChat.article_id.in_(article_ids),
            func.jsonb_array_length(ArticleAiChat.messages) > 0,
        )
    )
    return {r[0] for r in rows.all()}


def _build_filter_params(
    *,
    feed_id: int | None,
    folder_id: int | None,
    scope_include: str | None,
    label_id: int | None,
    unread_only: bool,
    starred_only: bool,
    archived_only: bool,
    saved_only: bool,
    labeled_only: bool,
    q: str | None,
    is_search: bool,
    sort_order: str,
    read_status: str | None,
    label_filter: str | None,
) -> dict:
    """Active-filter dict carried into infinite-scroll pagination. Shared by the
    first-page and load-more endpoints; the caller passes the unread flag it uses
    (effective_unread_only on the first page, the raw unread_only on load-more)."""
    params: dict = {}
    if feed_id is not None:
        params["feed_id"] = feed_id
    if folder_id is not None:
        params["folder_id"] = folder_id
    if scope_include:
        params["scope_include"] = scope_include
    if label_id is not None:
        params["label_id"] = label_id
    if unread_only:
        params["unread_only"] = "true"
    if starred_only:
        params["starred_only"] = "true"
    if archived_only:
        params["archived_only"] = "true"
    if saved_only:
        params["saved_only"] = "true"
    if labeled_only:
        params["labeled_only"] = "true"
    if q and q.strip():
        params["q"] = q.strip()
    if is_search:
        # Carry the search/filter knobs into pagination, even with an empty query.
        params["sort"] = sort_order
        if read_status:
            params["read_status"] = read_status
        if label_filter:
            params["label_filter"] = label_filter
    return params


def _build_more_qs(filter_params: dict, articles, q: str | None, next_offset: int) -> str:
    """Query string for the infinite-scroll "load more" sentinel.

    Search (FTS) keeps offset pagination (ts_rank ordering can't be keyset-paged,
    and search isn't unread-filtered). Everything else uses a keyset cursor on
    (sort_ts, id) so marking articles read mid-scroll can't shift the window and
    skip rows — see ix_articles_sort_ts.
    """
    params = dict(filter_params)
    if q and q.strip():
        params["offset"] = next_offset
    elif articles:
        params["cursor_ts"] = articles[-1].sort_ts.isoformat()
        params["cursor_id"] = articles[-1].id
    return urlencode(params)


@router.get("/htmx/articles", response_class=HTMLResponse)
async def htmx_article_list(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    scope_include: str | None = Query(None),
    label_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    saved_only: bool = Query(False),
    labeled_only: bool = Query(False),
    q: str | None = Query(None),
    sort: str | None = Query(None),
    read_status: str | None = Query(None),
    label_filter: str | None = Query(None),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The article list as a view of its own. Everything below is in render_list."""
    return await render_list(
        request, user=user, db=db,
        feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
        label_id=label_id, unread_only=unread_only, starred_only=starred_only,
        archived_only=archived_only, saved_only=saved_only, labeled_only=labeled_only,
        q=q, sort=sort, read_status=read_status, label_filter=label_filter,
        offset=offset,
    )


async def render_list(
    request: Request,
    *,
    user: User,
    db: AsyncSession,
    feed_id: int | None = None,
    folder_id: int | None = None,
    scope_include: str | None = None,
    label_id: int | None = None,
    unread_only: bool = False,
    starred_only: bool = False,
    archived_only: bool = False,
    saved_only: bool = False,
    labeled_only: bool = False,
    q: str | None = None,
    sort: str | None = None,
    read_status: str | None = None,
    label_filter: str | None = None,
    offset: int = 0,
) -> HTMLResponse:
    """Render the article list for one set of filters.

    Kept apart from the route so the other endpoint that answers with a list
    (htmx_save_url, which re-renders Saved after an import) can ask for one without
    calling a route handler. Called that way, FastAPI resolves nothing, so every
    ``Query(...)`` default arrives as a Query object — and those are truthy, which
    quietly switched archived_only and its neighbours on. Real defaults here mean a
    caller names only what it wants, and a filter added later reaches both entry
    points without anyone having to remember the second one.
    """
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()

    # The search modal can submit with an empty query term as a pure filter view
    # (scope / labels / status), so "search mode" is any of those, not just q.
    is_search = bool(q and q.strip()) or bool(scope_include) or bool(label_filter) or bool(read_status)

    sort_order = settings.default_sort_order if settings else "newest"
    # Search has its own sort selector (relevance default); other views use the
    # user's configured list sort. Without a query term relevance is meaningless,
    # so list_articles' non-FTS branch treats "relevance" as newest.
    if is_search:
        sort_order = sort or "relevance"
    articles_per_page = settings.articles_per_page if settings else 50
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    label_display = settings.label_display if settings else "indicator"
    is_mobile = _is_mobile(request)
    density = (settings.list_density_mobile if is_mobile else settings.list_density_web) if settings else "comfortable"

    # Resolve effective unread filter
    if is_search or starred_only or archived_only or saved_only:
        # Search uses its own status selector (read_status below); other
        # state-based views always show everything.
        effective_unread_only = False
    elif unread_only:
        # Explicit "Unread" nav item — always filter
        effective_unread_only = True
    else:
        unread_filter = settings.unread_filter if settings else "adaptive"
        if unread_filter == "unread_only":
            effective_unread_only = True
        elif unread_filter == "show_all":
            effective_unread_only = False
        else:  # adaptive
            probe = await list_articles(
                user=user, db=db,
                feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
                label_id=label_id,
                labeled_only=labeled_only,
                unread_only=True, limit=1,
            )
            effective_unread_only = len(probe) > 0

    articles = await list_articles(
        user=user,
        db=db,
        feed_id=feed_id,
        folder_id=folder_id,
        scope_include=scope_include,
        label_id=label_id,
        label_filter=label_filter,
        unread_only=effective_unread_only,
        read_status=read_status,
        starred_only=starred_only,
        archived_only=archived_only,
        saved_only=saved_only,
        labeled_only=labeled_only,
        q=q or None,
        sort_order=sort_order,
        limit=articles_per_page,
        offset=offset,
    )

    has_more = len(articles) >= articles_per_page

    # Title bar count for mobile hideable mode
    title_bar_count: int | None = None
    title_bar_count_type: str | None = None
    if label_id is not None:
        title_bar_count = (await db.execute(
            select(func.count(ArticleLabel.article_id))
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user.id))
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id == label_id,
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
        )).scalar() or 0
        title_bar_count_type = "unread"
    elif labeled_only:
        title_bar_count = (await db.execute(
            select(func.count(Article.id.distinct()))
            .select_from(Article)
            .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
            .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id))
            .where((UserArticleState.is_read == None) | (UserArticleState.is_read == False))
        )).scalar() or 0
        title_bar_count_type = "unread"
    elif starred_only:
        title_bar_count = (await db.execute(
            select(func.count(UserArticleState.article_id))
            .where(
                UserArticleState.user_id == user.id,
                UserArticleState.is_starred == True,
            )
        )).scalar() or 0
        title_bar_count_type = "starred"

    filter_params = _build_filter_params(
        feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
        label_id=label_id, unread_only=effective_unread_only,
        starred_only=starred_only, archived_only=archived_only, saved_only=saved_only,
        labeled_only=labeled_only,
        q=q, is_search=is_search, sort_order=sort_order,
        read_status=read_status, label_filter=label_filter,
    )

    extra_headers: dict[str, str] = {}
    if feed_id is not None:
        feed_obj = await db.get(Feed, feed_id)
        if feed_obj and feed_obj.status in ("error", "disabled") and feed_obj.last_error:
            extra_headers["HX-Trigger"] = json.dumps(
                {"showToast": {"msg": feed_obj.last_error[:150], "type": "warning"}}
            )

    extra_ctx: dict = {}
    if settings and getattr(settings, 'ai_chat_enabled', False):
        extra_ctx["chat_article_ids"] = await _get_chat_article_ids(
            user.id, [a.id for a in articles], db
        )

    if not articles and offset == 0:
        feed_count = await db.scalar(
            select(func.count(UserFeed.id)).where(UserFeed.user_id == user.id)
        )
        extra_ctx["has_feeds"] = bool(feed_count)
    else:
        extra_ctx["has_feeds"] = True

    list_html = templates.env.get_template("app/partials/article_list.html").render(
        request=request,
        articles=articles,
        feed_id=feed_id,
        folder_id=folder_id,
        unread_only=effective_unread_only,
        starred_only=starred_only,
        archived_only=archived_only,
        saved_view=saved_only,
        search_query=q.strip() if q and q.strip() else None,
        filter_active=is_search,
        # Text search uses offset pagination (ts_rank can't be keyset-paged). With a
        # read-status filter, marking rows read on scroll shrinks the result set
        # under the offset and skips articles, so disable mark-read-on-scroll for
        # that case only. Plain text search (status=all) and the empty-query filter
        # view (keyset pagination) are unaffected and keep it.
        mark_read_on_scroll=mark_read_on_scroll and not (q and q.strip() and read_status),
        density=density,
        label_display=label_display,
        show_ai_score=settings.ai_score_show_in_list if settings else False,
        has_more=has_more,
        more_qs=_build_more_qs(filter_params, articles, q, len(articles)),
        title_bar_count=title_bar_count,
        title_bar_count_type=title_bar_count_type,
        **extra_ctx,
    )
    oob = await _label_badge_oob(user.id, label_id, labeled_only, db)
    return HTMLResponse(list_html + oob, headers=extra_headers)


@router.get("/htmx/articles/more", response_class=HTMLResponse)
async def htmx_article_list_more(
    request: Request,
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    scope_include: str | None = Query(None),
    label_id: int | None = Query(None),
    unread_only: bool = Query(False),
    starred_only: bool = Query(False),
    archived_only: bool = Query(False),
    saved_only: bool = Query(False),
    labeled_only: bool = Query(False),
    q: str | None = Query(None),
    sort: str | None = Query(None),
    read_status: str | None = Query(None),
    label_filter: str | None = Query(None),
    offset: int = Query(0, ge=0),
    cursor_ts: datetime | None = Query(None),
    cursor_id: int | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()

    is_search = bool(q and q.strip()) or bool(scope_include) or bool(label_filter) or bool(read_status)
    sort_order = settings.default_sort_order if settings else "newest"
    if is_search:
        sort_order = sort or "relevance"
    articles_per_page = settings.articles_per_page if settings else 50
    is_mobile = _is_mobile(request)
    density = (settings.list_density_mobile if is_mobile else settings.list_density_web) if settings else "comfortable"
    label_display = settings.label_display if settings else "indicator"

    articles = await list_articles(
        user=user,
        db=db,
        feed_id=feed_id,
        folder_id=folder_id,
        scope_include=scope_include,
        label_id=label_id,
        label_filter=label_filter,
        unread_only=unread_only,
        read_status=read_status,
        starred_only=starred_only,
        archived_only=archived_only,
        saved_only=saved_only,
        labeled_only=labeled_only,
        q=q or None,
        sort_order=sort_order,
        limit=articles_per_page,
        offset=offset,
        cursor_ts=cursor_ts,
        cursor_id=cursor_id,
    )

    has_more = len(articles) >= articles_per_page
    filter_params = _build_filter_params(
        feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
        label_id=label_id, unread_only=unread_only,
        starred_only=starred_only, archived_only=archived_only, saved_only=saved_only,
        labeled_only=labeled_only,
        q=q, is_search=is_search, sort_order=sort_order,
        read_status=read_status, label_filter=label_filter,
    )

    extra_ctx = {}
    if settings and getattr(settings, 'ai_chat_enabled', False):
        extra_ctx["chat_article_ids"] = await _get_chat_article_ids(
            user.id, [a.id for a in articles], db
        )

    return templates.TemplateResponse(request, "app/partials/article_list_append.html", {
        "articles": articles,
        "density": density,
        "label_display": label_display,
        "show_ai_score": settings.ai_score_show_in_list if settings else False,
        "has_more": has_more,
        "more_qs": _build_more_qs(filter_params, articles, q, offset + len(articles)),
        **extra_ctx,
    })


@router.get("/htmx/articles/{article_id}", response_class=HTMLResponse)
async def htmx_article_detail(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Auto-trigger readable extraction if feed has it enabled and article wasn't extracted yet
    trigger_row = (await db.execute(
        select(
            Article.readable_status,
            Article.url,
            Feed.fetch_auth_user,
            Feed.fetch_auth_pass_encrypted,
            UserFeed.extract_readable,
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .where(Article.id == article_id)
    )).first()

    if (
        trigger_row is not None
        and trigger_row.extract_readable
        and trigger_row.readable_status == "skipped"
        and trigger_row.url
    ):
        # Reset retry bookkeeping: a user-initiated open is a fresh attempt, so the
        # article reads as "active" (spinner + poll) until this extraction resolves.
        await db.execute(
            sa_update(Article).where(Article.id == article_id).values(
                readable_status="pending",
                readable_retries=0,
                readable_next_retry_at=None,
            )
        )
        await db.commit()
        asyncio.create_task(_extract_readable_bg(
            article_id,
            trigger_row.url,
            trigger_row.fetch_auth_user,
            trigger_row.fetch_auth_pass_encrypted,
        ))

    article = await get_article(user, article_id, db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-4'>Article not found.</p>", status_code=404)
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    settings = settings_result.scalar_one_or_none()
    mark_read_on_scroll = settings.mark_read_on_scroll if settings else True
    ai = await _ai_availability(settings, db)
    ai_avail = ai.quality
    summary_pending = False
    if ai_avail and not article.ai_summary:
        summary_pending = bool(await db.scalar(
            select(ArticleAiJob.id).where(
                ArticleAiJob.article_id == article_id,
                ArticleAiJob.user_id == user.id,
                ArticleAiJob.operation == "summary",
                ArticleAiJob.status == "pending",
            )
        ))
    chat_available = ai.chat
    chat_messages: list[dict] = []
    if chat_available:
        existing_chat = await db.scalar(
            select(ArticleAiChat).where(
                ArticleAiChat.article_id == article_id,
                ArticleAiChat.user_id == user.id,
            )
        )
        if existing_chat and existing_chat.messages:
            chat_messages = list(existing_chat.messages)
    return templates.TemplateResponse(request, "app/partials/article_detail.html", {
        "article": article,
        "mark_read_on_scroll": mark_read_on_scroll,
        "ai_available": ai_avail,
        "summary_pending": summary_pending,
        "chat_available": chat_available,
        "chat_messages": chat_messages,
    })


@router.get("/htmx/articles/{article_id}/readable-poll", response_class=HTMLResponse)
async def htmx_readable_poll(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Polling endpoint for a running readable extraction.

    While the extraction runs, only the small progress strip is returned — swapping
    the whole content block every 2s relaid out the article (images, bottom bar) and
    made it jump. Once it finishes, the full content block is returned and the swap
    is retargeted at the content container, so the article is rebuilt exactly once.
    """
    article = await get_article(user, article_id, db)
    if not article:
        return HTMLResponse("", status_code=404)
    if article.readable_active:
        return HTMLResponse(
            templates.env.get_template("app/partials/readable_progress.html").render(
                request=request, article=article
            )
        )
    response = _content_with_readtime_oob(
        request, article, extra_oob=await _summary_refresh_oob(article, user, db)
    )
    response.headers["HX-Retarget"] = f"#article-content-{article.id}"
    response.headers["HX-Reswap"] = "outerHTML"
    return response


@router.get("/htmx/articles/{article_id}/row-poll", response_class=HTMLResponse)
async def htmx_row_poll(
    article_id: int,
    request: Request,
    density: str | None = Query(None),
    label_display: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Polling endpoint for a saved article's list row while extraction runs.

    A saved-by-URL article is inserted with a placeholder title and only learns its
    real one (plus a snippet, and sometimes a publication date) when extraction
    finishes — after the row was rendered. While it runs the poller answers with
    itself so it keeps ticking; once done it returns the rebuilt row, retargeted at
    the row container, and the polling stops with it.
    """
    from app.services.article import get_article_list_item

    item = await get_article_list_item(user, article_id, db)
    if item is None:
        # Gone (unsaved, purged, or access revoked) — drop the poller.
        return HTMLResponse("")
    if item.readable_active:
        # The same element the row rendered, so the loop carries on unchanged.
        macros = templates.env.get_template("app/partials/row_poll.html").module
        return HTMLResponse(
            str(macros.row_poll(article_id, density or "", label_display or ""))
        )

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    row_html = templates.env.get_template("app/partials/article_row.html").render(
        request=request,
        article=item,
        density=density or (settings.list_density_web if settings else "comfortable"),
        label_display=label_display or (settings.label_display if settings else "indicator"),
        show_ai_score=settings.ai_score_show_in_list if settings else False,
    )
    response = HTMLResponse(row_html)
    response.headers["HX-Retarget"] = f"#article-row-{article_id}"
    response.headers["HX-Reswap"] = "outerHTML"
    return response


async def _summary_refresh_oob(article, user: User, db: AsyncSession) -> str:
    """OOB refresh of the AI summary block, or "" when there is nothing to show.

    Finishing a readable extraction runs the AI pipeline, so a summary can be
    produced (or queued) after the detail was rendered. Without this the reader is
    left with an empty spot and only sees the summary by reopening the article.
    """
    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    ai = await _ai_availability(settings, db)
    if not ai.quality:
        return ""
    macros = templates.env.get_template("app/partials/ai_blocks.html").module
    if article.ai_summary:
        return str(macros.ai_summary(
            article.id, article.ai_summary, article.ai_summary_truncated, oob=True
        ))
    pending = await db.scalar(
        select(ArticleAiJob.id).where(
            ArticleAiJob.article_id == article.id,
            ArticleAiJob.user_id == user.id,
            ArticleAiJob.operation == "summary",
            ArticleAiJob.status == "pending",
        )
    )
    if not pending:
        return ""
    return str(macros.ai_spinner(
        f"ai-summary-{article.id}",
        f"/htmx/articles/{article.id}/ai-summary/poll",
        "Generating summary…",
        oob=True,
    ))


def _content_with_readtime_oob(request: Request, article, extra_oob: str = "") -> HTMLResponse:
    """Return article_content.html + OOB span to update the reading-time metadata."""
    content_html = templates.env.get_template("app/partials/article_content.html").render(
        request=request, article=article, chat_available=False
    )
    read_time = f"· {article.estimated_read_min} min read" if article.estimated_read_min else ""
    oob = (
        f'<span id="article-meta-readtime-{article.id}" class="shrink-0"'
        f' hx-swap-oob="true">{read_time}</span>'
    )
    # Refresh the publication date too: readable extraction may have backfilled
    # published_at (via htmldate) since the detail was first rendered.
    date_oob = templates.env.get_template("app/partials/article_meta_date.html").render(
        request=request, article=article, oob=True
    )
    # And the heading: a saved-by-URL article starts out titled with its host + path
    # and gets its real title from the page, which lands after the detail rendered.
    # Only feedless articles can change title, so nothing else is touched.
    title_oob = ""
    if article.feed_id is None:
        macros = templates.env.get_template("app/partials/article_title.html").module
        title_oob = str(macros.article_title(article, oob=True))
    return HTMLResponse(content_html + oob + date_oob + title_oob + extra_oob)


def _state_button_response(
    request: Request, article, *, template: str, event: str, payload: dict,
    extra_events: dict | None = None,
) -> HTMLResponse:
    """Render a state button/icon partial + fire an HX-Trigger event (no OOB row
    swap, to avoid flicker). Shared by the read / star / archive toggles."""
    btn_html = templates.env.get_template(template).render(article=article, request=request)
    response = HTMLResponse(btn_html)
    events = {"sidebarRefresh": True, event: payload}
    if extra_events:
        events.update(extra_events)
    response.headers["HX-Trigger"] = json.dumps(events)
    return response


def _read_response(request: Request, article) -> HTMLResponse:
    return _state_button_response(
        request, article,
        template="app/partials/read_button.html",
        event="articleReadChanged",
        payload={"id": article.id, "isRead": article.is_read},
    )


def _star_response(request: Request, article, *, summary_started: bool = False) -> HTMLResponse:
    # summaryStarted lets an open article swap in the "Generating summary…" spinner;
    # the summary runs in the background, so otherwise nothing on screen says so.
    return _state_button_response(
        request, article,
        template="app/partials/star_icon.html",
        event="articleStarChanged",
        payload={"id": article.id, "isStarred": article.is_starred},
        extra_events={"summaryStarted": {"id": article.id}} if summary_started else None,
    )


def _archive_response(request: Request, article) -> HTMLResponse:
    return _state_button_response(
        request, article,
        template="app/partials/archive_button.html",
        event="articleArchiveChanged",
        payload={"id": article.id, "isArchived": article.is_archived},
    )


@router.post("/htmx/articles/{article_id}/read", response_class=HTMLResponse)
async def htmx_toggle_read(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(user, article_id, "is_read", db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    return _read_response(request, article)


@router.post("/htmx/articles/{article_id}/set-read", response_class=HTMLResponse)
async def htmx_set_read(
    article_id: int,
    request: Request,
    state: bool = Query(True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await update_article_state(user, article_id, ArticleStateUpdate(is_read=state), db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    return _read_response(request, article)


@router.post("/htmx/articles/{article_id}/dwell")
async def htmx_article_dwell(
    article_id: int,
    seconds: int = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    seconds = max(0, min(seconds, 1800))  # cap at 30 minutes per session
    if seconds <= 3:
        return HTMLResponse("", status_code=204)
    if not await filter_accessible_article_ids(user.id, [article_id], db):
        return HTMLResponse("", status_code=404)
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    # Upsert: an article read in the detail panel has no state row yet (mark-read
    # fires later on scroll-off), so a plain UPDATE would silently drop the dwell.
    stmt = (
        pg_insert(UserArticleState)
        .values(user_id=user.id, article_id=article_id, dwell_seconds=seconds)
        .on_conflict_do_update(
            index_elements=["user_id", "article_id"],
            set_={"dwell_seconds": UserArticleState.dwell_seconds + seconds},
        )
    )
    await db.execute(stmt)
    await db.commit()
    return HTMLResponse("", status_code=204)


@router.post("/htmx/articles/{article_id}/link-opened")
async def htmx_article_link_opened(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.article_id == article_id,
            UserArticleState.user_id == user.id,
        )
    )
    if state is not None and not state.link_opened:
        state.link_opened = True
        await db.commit()
    return HTMLResponse("", status_code=204)


@router.post("/htmx/articles/{article_id}/star", response_class=HTMLResponse)
async def htmx_toggle_star(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(user, article_id, "is_starred", db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)

    summary_started = False
    if article.is_starred:
        settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
        if settings and settings.ai_summary_enabled_default:
            article_obj = await db.scalar(select(Article).where(Article.id == article_id))
            if article_obj is not None:
                from app.services.ai_summary_service import enqueue_summary_job
                enqueued = await enqueue_summary_job(article_obj, user.id, db)
                await db.commit()
                if enqueued:
                    summary_started = True
                    asyncio.create_task(_summary_after_star_bg(article_id, user.id))
    else:
        # Unstarred — cancel a not-yet-run summary job so a mis-click doesn't
        # produce (and bill) a summary via the debounce task or the batch worker.
        await db.execute(
            sa_delete(ArticleAiJob).where(
                ArticleAiJob.article_id == article_id,
                ArticleAiJob.user_id == user.id,
                ArticleAiJob.operation == "summary",
                ArticleAiJob.status == "pending",
            )
        )
        await db.commit()

    return _star_response(request, article, summary_started=summary_started)


@router.post("/htmx/articles/{article_id}/archive", response_class=HTMLResponse)
async def htmx_toggle_archive(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(user, article_id, "is_archived", db)
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    return _archive_response(request, article)


@router.get("/htmx/articles/{article_id}/labels", response_class=HTMLResponse)
async def htmx_article_labels(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    all_labels = await list_labels(user, db)
    assigned: set[int] = set((await db.execute(
        select(ArticleLabel.label_id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user.id)
    )).scalars())
    return templates.TemplateResponse(request, "app/partials/label_picker.html", {
        "article_id": article_id,
        "all_labels": all_labels,
        "assigned": assigned,
        "show_oob": False,
        "assigned_labels": [],
    })


@router.post("/htmx/articles/{article_id}/labels/{label_id}/toggle", response_class=HTMLResponse)
async def htmx_toggle_article_label(
    article_id: int,
    label_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.models.label import Label
    from app.services.label_service import assign_label, remove_label

    existing = (await db.execute(
        select(ArticleLabel).where(
            ArticleLabel.article_id == article_id,
            ArticleLabel.label_id == label_id,
            ArticleLabel.user_id == user.id,
        )
    )).scalar_one_or_none()

    if existing:
        ok = await remove_label(user, article_id, label_id, db)
    else:
        ok = await assign_label(user, article_id, label_id, db)

    if not ok:
        return HTMLResponse("", status_code=404)

    all_labels = await list_labels(user, db)
    assigned_labels_rows = (await db.execute(
        select(Label.id, Label.name, Label.color)
        .join(ArticleLabel, ArticleLabel.label_id == Label.id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user.id)
        .order_by(Label.position, func.lower(Label.name))
    )).all()
    assigned_labels = [{"id": r[0], "name": r[1], "color": r[2]} for r in assigned_labels_rows]
    assigned: set[int] = {l["id"] for l in assigned_labels}

    settings_result = await db.execute(select(UserSettings).where(UserSettings.user_id == user.id))
    settings = settings_result.scalar_one_or_none()
    label_display = settings.label_display if settings else "indicator"

    picker_html = templates.env.get_template("app/partials/label_picker.html").render(
        request=request,
        article_id=article_id,
        all_labels=all_labels,
        assigned=assigned,
        show_oob=True,
        assigned_labels=assigned_labels,
        label_display=label_display,
    )
    response = HTMLResponse(picker_html)
    response.headers["HX-Trigger"] = "sidebarRefresh"
    return response


@router.post("/htmx/articles/{article_id}/share", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_share_token)
async def htmx_toggle_share(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Toggle share token for an article. Generates on first call, revokes on second."""
    # Load article access + state
    stmt = add_article_access_joins(
        select(Article, UserArticleState), user.id
    ).where(Article.id == article_id, article_access_predicate())
    row = (await db.execute(stmt)).first()
    if not row:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)

    article, state = row
    if state is None:
        state = UserArticleState(user_id=user.id, article_id=article_id)
        db.add(state)

    if state.share_token:
        state.share_token = None
        share_url = None
    else:
        state.share_token = secrets.token_urlsafe(24)
        share_url = str(request.base_url) + f"share/{state.share_token}"

    await db.commit()
    await db.refresh(state)

    return templates.TemplateResponse(request, "app/partials/share_button.html", {
        "article": type("A", (), {"id": article_id, "share_token": state.share_token})(),
        "share_url": share_url,
    })


@router.post("/htmx/articles/{article_id}/extract-readable", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_extract_readable)
async def htmx_extract_readable(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Extract readable content on demand for a single article."""
    from app.services.readable_service import (
        extract_readable_with_title, store_saved_extraction,
    )
    from app.utils.crypto import feed_auth

    stmt = add_article_access_joins(
        select(Article, Feed.fetch_auth_user, Feed.fetch_auth_pass_encrypted)
        .outerjoin(Feed, Feed.id == Article.feed_id),
        user.id,
    ).where(Article.id == article_id, article_access_predicate())
    row = (await db.execute(stmt)).first()
    if not row:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)

    article, auth_user, auth_pass_enc = row
    if not article.url:
        return HTMLResponse("<p class='text-amber-500 p-2 text-xs'>Article has no URL.</p>")

    if article.readable_status == "success":
        return HTMLResponse("")  # already done, nothing to do

    auth_user, auth_pass = feed_auth(
        auth_user, auth_pass_enc, context=f"article {article.id}"
    ) or (None, None)

    loop = asyncio.get_running_loop()
    # Ask for the title too: on a feedless saved article the page is the only source
    # of one, so a retry should refresh it. apply_readable_result ignores it for feed
    # articles, which keep their feed-supplied title.
    result = await loop.run_in_executor(
        None, extract_readable_with_title, article.url, auth_user, auth_pass,
        article.feed_id is None,  # consent/paywall check: saved articles only
    )

    if article.feed_id is None:
        # The third door into a terminal state, after the import task and the batch
        # worker. A saved article's post-extraction pass (its saver's filters, and the
        # summary those may trigger) belongs to every one of them, so this goes through
        # the helper the batch worker uses rather than writing the steps out again —
        # which is how this door came to be the one missing the last of them. Reachable
        # whenever a transient failure leaves the article 'pending': the batch worker
        # only ever picks up that status, so once Retry succeeds here nothing would
        # come back for it.
        await store_saved_extraction(article, result, db)
    else:
        # A feed article keeps its feed-supplied title and never adopts a resolved
        # address, so the two arguments above are not passed and adopt_resolved_url is
        # not called: both are no-ops on this branch by construction.
        apply_readable_result(
            article, result.content, result.error, result.http_status, result.published_at,
        )
        await db.commit()

    # Render from the full ArticleResponse (not the raw ORM row) so per-user fields
    # — is_starred/is_archived/labels/readable_active — render correctly in the
    # re-swapped content block; the ORM Article lacks them.
    article_resp = await get_article(user, article_id, db)
    if article_resp is None:
        return HTMLResponse("")
    return _content_with_readtime_oob(request, article_resp)


@router.post("/htmx/articles/save-url", response_class=HTMLResponse)
@limiter.limit(app_settings_config.rate_limit_save_url)
async def htmx_save_url(
    request: Request,
    url: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Save a pasted URL as a standalone article and re-render the Saved list."""
    from app.services.saved_article_service import save_article_by_url

    toast: dict | None = None
    saved_id: int | None = None
    try:
        article, already_known = await save_article_by_url(url.strip(), user, db)
    except ValueError as exc:
        # Validation-time rejections only — bad scheme, no host, unresolvable, or a
        # private/loopback address. Anything that can only fail once the fetch runs
        # (404, timeout, paywall) is saved and surfaces in the detail panel instead.
        toast = {"msg": str(exc), "type": "error"}
    else:
        saved_id = article.id
        if already_known:
            # already_known says the article was already in the database, which is true
            # of anything that ever came in through a feed — including someone else's.
            # It says nothing about Saved, so neither does this: reading it as "you had
            # already saved this" is wrong for the common case of pasting a link to an
            # article from a feed you subscribe to.
            toast = {"msg": "Saved. Readfine already had this article.", "type": "info"}

    response = await render_list(request, user=user, db=db, saved_only=True)
    events: dict = {}
    if toast:
        events["showToast"] = toast
    if saved_id is not None:
        # The list is ordered by publication date, not by when you saved, so an older
        # article (typically a video from a feed you follow, carrying the date it was
        # published) lands somewhere down the list instead of on top. Tell the client
        # which row to point at.
        #
        # Plain HX-Trigger, which fires before the swap: this form lives inside
        # #article-list and the swap removes it, and an event dispatched on a detached
        # element never reaches document.body, so HX-Trigger-After-Settle would go
        # nowhere. The handler waits for the settle itself.
        events["savedArticleAdded"] = {"id": saved_id}
    if events:
        response.headers["HX-Trigger"] = json.dumps(events)
    return response


@router.post("/htmx/articles/{article_id}/unsave", response_class=HTMLResponse)
async def htmx_unsave_article(
    article_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Remove an article from Saved. Never deletes the (globally shared) article row."""
    from app.services.saved_article_service import unsave_article

    await unsave_article(article_id, user.id, db)
    response = HTMLResponse("")
    response.headers["HX-Trigger"] = json.dumps({
        "savedArticleRemoved": {"id": article_id},
        "sidebarRefresh": True,
    })
    return response

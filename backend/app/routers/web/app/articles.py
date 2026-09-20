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
from sqlalchemy import case, delete as sa_delete, func, null, select, update as sa_update
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
from app.services.story_service import (
    DEDUP_COLLAPSE,
    DEDUP_OFF,
    ENGAGED_DWELL_SECONDS,
    MEMBER_LIMIT,
    row_count,
    annotate as annotate_stories,
    collapse_page,
    count_members,
    list_members,
    next_shown as next_shown_stories,
    parse_shown as parse_shown_stories,
)
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
    # Rows whose story is unfolded in the list right now. They are read one by one
    # like any other row and do not close the rest of their group; the browser is the
    # only place that knows which those are.
    unfolded = [int(i) for i in (data.get("unfolded") or [])[:500] if str(i).isdigit()]
    await mark_articles_read_batch(user, ids, db, unfolded_ids=unfolded)
    return HTMLResponse("", status_code=200)


async def _label_badge_oob(user_id: int, label_id: int | None, labeled_only: bool, db: AsyncSession) -> str:
    """Return OOB HTML snippets to update label badge(s) in the sidebar."""
    if not label_id and not labeled_only:
        return ""
    oob = ""
    # Rows, not articles: a label view folds a story into one row like the other
    # reading views, and these badges stand above it (story_service.row_count). Unless
    # the reader has the feature off, in which case their list folds nothing.
    story_dedup = await db.scalar(
        select(UserSettings.story_dedup).where(UserSettings.user_id == user_id)
    )
    rows_drawn = row_count(story_dedup != DEDUP_OFF)
    # Every one of these carries trimmed_at IS NULL, the filter list_articles applies
    # and the sidebar counters already had: a retention stub is gone from the list, so
    # a badge that still counted it would stand above fewer rows than it claims.
    if label_id:
        lu = (await db.scalar(
            select(rows_drawn)
            .select_from(ArticleLabel)
            .join(Article, Article.id == ArticleLabel.article_id)
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user_id))
            .where(
                ArticleLabel.user_id == user_id,
                ArticleLabel.label_id == label_id,
                Article.trimmed_at.is_(None),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
        )) or 0
        lt = (await db.scalar(
            select(rows_drawn)
            .select_from(ArticleLabel)
            .join(Article, Article.id == ArticleLabel.article_id)
            .where(
                ArticleLabel.user_id == user_id,
                ArticleLabel.label_id == label_id,
                Article.trimmed_at.is_(None),
            )
        )) or 0
        oob += f'<span id="label-badge-{label_id}" hx-swap-oob="innerHTML">{_badge_html(lu, lt)}</span>'
    # Aggregate "Labels" badge
    all_unread = (await db.scalar(
        select(rows_drawn)
        .select_from(Article)
        .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user_id))
        .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id))
        .where(
            Article.trimmed_at.is_(None),
            (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
        )
    )) or 0
    all_total = (await db.scalar(
        select(rows_drawn)
        .select_from(ArticleLabel)
        .join(Article, Article.id == ArticleLabel.article_id)
        .where(ArticleLabel.user_id == user_id, Article.trimmed_at.is_(None))
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


def _build_more_qs(
    filter_params: dict, articles, q: str | None, next_offset: int,
    shown_stories: list[int] | None = None,
) -> str:
    """Query string for the infinite-scroll "load more" sentinel.

    Search (FTS) keeps offset pagination (ts_rank ordering can't be keyset-paged,
    and search isn't unread-filtered). Everything else uses a keyset cursor on
    (sort_ts, id) so marking articles read mid-scroll can't shift the window and
    skip rows — see ix_articles_sort_ts.

    ``articles`` has to be the page as the query returned it, before story collapsing
    drops the folded-away rows. Taking the cursor off the last row still on screen
    would ask the next page to start again in the middle of the page just rendered,
    and the members that folded into a row here would come back as rows of their own
    there — the representative they belong under is no longer in the window.

    ``shown_stories`` travels with the cursor for the same reason and is the other half
    of it: the cursor says where to read on, this says which stories already have a row
    above. Carrying it in the address keeps the server out of it — the state belongs to
    one scroll through one list, and a reload starts a fresh one.
    """
    params = dict(filter_params)
    if q and q.strip():
        params["offset"] = next_offset
    elif articles:
        params["cursor_ts"] = articles[-1].sort_ts.isoformat()
        params["cursor_id"] = articles[-1].id
    if shown_stories:
        params["shown_stories"] = ",".join(str(i) for i in shown_stories)
    return urlencode(params)


def _collapses_stories(
    *, story_dedup: str, feed_id: int | None, starred_only: bool,
    archived_only: bool, saved_only: bool,
) -> bool:
    """Whether this view folds the other coverage of a story into one row.

    Nothing folds when the reader has the feature off: the setting is what decides
    whether the list is theirs to shape at all, and the view only decides where that
    shaping makes sense.

    The reading views do, search included: a search for a story that five newsrooms
    filed answered with five rows saying the same thing, and folding only ever hides a
    row that did match, under the best-matching one, with the marker saying it is
    there. That last part is why search waited for the list to be able to unfold a
    group — until then the only way to the folded article led through the article
    above it, which is too far for a view whose job is to answer "is this in here".

    Starred, saved and archive do not: those are lists the reader assembled by hand,
    and a row missing from one of them is a row they put there themselves. Nor does a
    single feed, which is a question about that feed, and hiding one of its articles
    because another source filed first answers a different one.
    """
    if story_dedup == DEDUP_OFF:
        return False
    return not (feed_id is not None or starred_only or archived_only or saved_only)


def story_scope(
    *, feed_id=None, folder_id=None, scope_include=None, label_id=None,
    labeled_only=False, label_filter=None, q=None,
) -> dict:
    """The filters that decide which members of a story belong in this view.

    Structure, not state. A label, a folder or a search term says what the list is
    about, so a member that does not match is not this list's article and unfolding
    one into it would be putting something there the reader did not ask for — and
    those rows mark themselves read on scroll, so it would also quietly take it off
    their unread list. Read/unread, starred, saved and archived are deliberately not
    here: those describe what the reader has done with an article, and a group is
    worth opening precisely because it is usually read in pieces.

    Empty means the view holds whole groups, and the caller then skips the extra query
    entirely. That is the common case, the main unread list.
    """
    scope = {}
    if feed_id is not None:
        scope["feed_id"] = feed_id
    if folder_id is not None:
        scope["folder_id"] = folder_id
    if scope_include:
        scope["scope_include"] = scope_include
    if label_id is not None:
        scope["label_id"] = label_id
    elif labeled_only:
        scope["labeled_only"] = True
    if label_filter:
        scope["label_filter"] = label_filter
    if q and q.strip():
        scope["q"] = q
    return scope


# Ceiling on the member lookup that scoped views do. A page is at most a few dozen
# rows, only some of them carry a story, and a story is capped well below this, so it
# is a guard against a pathological page rather than a limit anything reaches.
_SCOPE_MEMBER_LIMIT = 1000


async def _members_in_scope(
    rows: list, user: User, db: AsyncSession, scope: dict
) -> set[int] | None:
    """Which members of this page's stories the view's own filters would give back.

    None means "all of them", which is both the unfiltered case and the cheap one: no
    scope, no query. Everything else asks the list itself, with the view's filters and
    none of its state, so the answer cannot drift from what unfolding actually returns.
    """
    story_ids = {r.story_id for r in rows if r.story_id is not None}
    if not story_ids or not scope:
        return None
    members = await list_articles(
        user=user, db=db, story_ids=list(story_ids),
        limit=_SCOPE_MEMBER_LIMIT, **scope,
    )
    return {m.id for m in members}


async def _apply_story_collapse(
    rows: list, user: User, db: AsyncSession, *, collapse: bool,
    story_dedup: str = DEDUP_COLLAPSE, shown_stories: list[int] | None = None,
    scope: dict | None = None,
) -> tuple[list, list[int]]:
    """Fold the page's stories (when the view does that) and annotate what is left.

    Returns the rows to render and the story list for the next page's address.

    The annotation runs either way: a view that does not collapse still marks a row
    that has other coverage behind it, so the reader can tell before opening it. The
    story list is only kept where the view folds, since that is the only place a later
    page has to know what came before.

    With the feature off the rows come back untouched and unmarked. Off means the list
    looks like it did before any of this existed, not "folds nothing but still points
    at what it would have folded".
    """
    if story_dedup == DEDUP_OFF:
        return rows, []
    if not collapse:
        # A view that folds nothing still marks what has coverage behind it, and it
        # says so about the whole group: there is no unfolding here to keep honest,
        # and the footer of the article shows the group whole anyway.
        await annotate_stories(rows, user.id, db)
        return rows, []
    articles = collapse_page(rows, shown_stories)
    in_scope = await _members_in_scope(articles, user, db, scope or {})
    await annotate_stories(articles, user.id, db, in_scope)
    return articles, next_shown_stories(shown_stories or [], articles)


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
    story_dedup = settings.story_dedup if settings else DEDUP_COLLAPSE
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

    rows = await list_articles(
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

    # has_more counts what the query returned, not what survives collapsing: a full
    # page means there is more behind it even if half of it folded into one row.
    has_more = len(rows) >= articles_per_page
    collapses = _collapses_stories(
        story_dedup=story_dedup, feed_id=feed_id, starred_only=starred_only,
        archived_only=archived_only, saved_only=saved_only,
    )
    articles, shown_stories = await _apply_story_collapse(
        rows, user, db, collapse=collapses, story_dedup=story_dedup,
        scope=story_scope(
            feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
            label_id=label_id, labeled_only=labeled_only, label_filter=label_filter, q=q,
        ),
    )

    # Title bar count for mobile hideable mode
    rows_drawn = row_count(story_dedup != DEDUP_OFF)
    title_bar_count: int | None = None
    title_bar_count_type: str | None = None
    if label_id is not None:
        title_bar_count = (await db.execute(
            select(rows_drawn)
            .select_from(ArticleLabel)
            .join(Article, Article.id == ArticleLabel.article_id)
            .outerjoin(UserArticleState,
                (UserArticleState.article_id == ArticleLabel.article_id) &
                (UserArticleState.user_id == user.id))
            .where(
                ArticleLabel.user_id == user.id,
                ArticleLabel.label_id == label_id,
                Article.trimmed_at.is_(None),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
        )).scalar() or 0
        title_bar_count_type = "unread"
    elif labeled_only:
        title_bar_count = (await db.execute(
            select(rows_drawn)
            .select_from(Article)
            .join(ArticleLabel, (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
            .outerjoin(UserArticleState, (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id))
            .where(
                Article.trimmed_at.is_(None),
                (UserArticleState.is_read == None) | (UserArticleState.is_read == False),
            )
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
        # Search never marks rows read on scroll. Looking something up is not
        # reading it: the reader scans the results for the one they want, and the
        # rest should keep the state they had. It also sidesteps a pagination bug,
        # since text search pages by offset (ts_rank can't be keyset-paged) and a
        # read-status filter shrinking the result set under that offset skips rows.
        mark_read_on_scroll=mark_read_on_scroll and not is_search,
        density=density,
        label_display=label_display,
        show_ai_score=settings.ai_score_show_in_list if settings else False,
        # A row offers to unfold its story only where the list folded one — see
        # _collapses_stories. Everywhere else the row keeps the quiet marker instead.
        story_unfoldable=collapses,
        story_scope_qs=urlencode(story_scope(
            feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
            label_id=label_id, labeled_only=labeled_only, label_filter=label_filter, q=q,
        )),
        has_more=has_more,
        # Cursor off the raw page, see _build_more_qs.
        more_qs=_build_more_qs(filter_params, rows, q, len(rows), shown_stories),
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
    # Stories the pages above already have a row for, put there by the sentinel this
    # request came from. Kept as a string and parsed in the service: it is a list the
    # client hands back, so its length and contents are checked rather than declared.
    shown_stories: str | None = Query(None),
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
    story_dedup = settings.story_dedup if settings else DEDUP_COLLAPSE

    rows = await list_articles(
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

    has_more = len(rows) >= articles_per_page
    collapses = _collapses_stories(
        story_dedup=story_dedup, feed_id=feed_id, starred_only=starred_only,
        archived_only=archived_only, saved_only=saved_only,
    )
    articles, next_stories = await _apply_story_collapse(
        rows, user, db, collapse=collapses, story_dedup=story_dedup,
        shown_stories=parse_shown_stories(shown_stories),
        scope=story_scope(
            feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
            label_id=label_id, labeled_only=labeled_only, label_filter=label_filter, q=q,
        ),
    )
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
        # Same rule as the first page, see render_list.
        "story_unfoldable": collapses,
        "story_scope_qs": urlencode(story_scope(
            feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
            label_id=label_id, labeled_only=labeled_only, label_filter=label_filter, q=q,
        )),
        "has_more": has_more,
        # Cursor off the raw page, see _build_more_qs.
        "more_qs": _build_more_qs(
            filter_params, rows, q, offset + len(rows), next_stories
        ),
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
    story_dedup = settings.story_dedup if settings else DEDUP_COLLAPSE
    related_count = (
        0 if story_dedup == DEDUP_OFF
        else await count_members(user.id, article.story_id, article_id, db)
    )
    return templates.TemplateResponse(request, "app/partials/article_detail.html", {
        "article": article,
        "mark_read_on_scroll": mark_read_on_scroll,
        "ai_available": ai_avail,
        "summary_pending": summary_pending,
        "chat_available": chat_available,
        "chat_messages": chat_messages,
        "related_count": related_count,
    })


async def _story_dedup_off(user_id: int, db: AsyncSession) -> bool:
    """Has this reader turned story grouping off?

    The two unfold endpoints ask before answering. Nothing in the page offers the
    control when the feature is off, so this is not a gate anybody reaches by clicking;
    it is there so the setting means the same thing everywhere, and a stale page left
    open across a change in Settings cannot unfold a group the reader has said they do
    not want.
    """
    return (await db.scalar(
        select(UserSettings.story_dedup).where(UserSettings.user_id == user_id)
    )) == DEDUP_OFF


@router.get("/htmx/articles/{article_id}/related", response_class=HTMLResponse)
async def htmx_article_related(
    article_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The rest of the coverage of this article's story, unfolded on click.

    Access is checked twice on purpose: once for the article being read, and again, per
    member, inside the service. The group is global, so the second check is the one that
    keeps a feed the reader never subscribed to out of the footer.
    """
    story_id = (await db.execute(
        add_article_access_joins(select(Article.story_id), user.id)
        .where(Article.id == article_id, article_access_predicate())
    )).scalar_one_or_none()
    if story_id is None or await _story_dedup_off(user.id, db):
        return HTMLResponse("")

    members = await list_members(user.id, story_id, article_id, db)
    return templates.TemplateResponse(request, "app/partials/story_members.html", {
        "article_id": article_id,
        "members": members,
    })


@router.get("/htmx/articles/{article_id}/story-rows", response_class=HTMLResponse)
async def htmx_article_story_rows(
    article_id: int,
    request: Request,
    density: str | None = Query(None),
    label_display: str | None = Query(None),
    feed_id: int | None = Query(None),
    folder_id: int | None = Query(None),
    scope_include: str | None = Query(None),
    label_id: int | None = Query(None),
    labeled_only: bool = Query(False),
    label_filter: str | None = Query(None),
    q: str | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The rest of this article's story as list rows, to sit under the row it came from.

    Rows rather than the footer's short entries: a member opens from the list the way
    every other row opens, into the detail panel, so the list needs nothing of its own
    to open articles with. That is also why these are real ``.article-row`` elements,
    marked read on scroll like their neighbours — the rule against that applies to rows
    that are in the DOM without being on screen, and these are only ever inserted
    because the reader asked to see them.

    **State** is deliberately ignored: a group is shown whole, read members included,
    even where the view around it is filtered to unread. The count on the row promised
    that many, and a group is usually read in pieces, which is the reason to look at it.

    **Structure** is not, and the difference matters because of the paragraph above.
    The view's own filters come along, so a label list gives back the members carrying
    that label and a search the ones that match. Those are the rows it folded, so
    unfolding is the exact inverse of folding; without it a label list would hand back
    articles that were never in it and then mark them read as the reader scrolled past.
    The whole group is still one click away, in the footer of the article.
    """
    story_id = (await db.execute(
        add_article_access_joins(select(Article.story_id), user.id)
        .where(Article.id == article_id, article_access_predicate())
    )).scalar_one_or_none()
    if story_id is None:
        return HTMLResponse("")

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    if settings is not None and settings.story_dedup == DEDUP_OFF:
        return HTMLResponse("")
    members = await list_articles(
        user=user, db=db, story_id=story_id,
        sort_order=settings.default_sort_order if settings else "newest",
        limit=MEMBER_LIMIT + 1,
        **story_scope(
            feed_id=feed_id, folder_id=folder_id, scope_include=scope_include,
            label_id=label_id, labeled_only=labeled_only, label_filter=label_filter, q=q,
        ),
    )
    rows = [m for m in members if m.id != article_id]
    if not rows:
        return HTMLResponse("")

    extra_ctx: dict = {}
    if settings and getattr(settings, "ai_chat_enabled", False):
        extra_ctx["chat_article_ids"] = await _get_chat_article_ids(
            user.id, [r.id for r in rows], db
        )
    return templates.TemplateResponse(request, "app/partials/story_rows.html", {
        "articles": rows,
        "parent_id": article_id,
        "density": density or (settings.list_density_web if settings else "comfortable"),
        "label_display": label_display or (settings.label_display if settings else "indicator"),
        "show_ai_score": settings.ai_score_show_in_list if settings else False,
        **extra_ctx,
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
    response = await _content_with_readtime_oob(
        request, article, user, db,
        extra_oob=await _summary_refresh_oob(article, user, db),
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
    # The row this one was unfolded from, when it is a member of a story rather than a
    # row of the list proper. Carried by the poller so the rebuilt row keeps its indent
    # and its data-story-parent; without it a member whose extraction finished would
    # jump back to the left margin and stop counting as part of the group.
    story_parent: int | None = Query(None),
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
            str(macros.row_poll(
                article_id, density or "", label_display or "", story_parent or ""
            ))
        )

    # Rebuilding the row means rebuilding everything on it, the story marker included,
    # or finishing an extraction would silently take the marker away.
    await annotate_stories([item], user.id, db)

    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
    row_html = templates.env.get_template("app/partials/article_row.html").render(
        request=request,
        article=item,
        density=density or (settings.list_density_web if settings else "comfortable"),
        label_display=label_display or (settings.label_display if settings else "indicator"),
        show_ai_score=settings.ai_score_show_in_list if settings else False,
        story_child=story_parent,
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


async def _content_with_readtime_oob(
    request: Request, article, user: User, db: AsyncSession, extra_oob: str = ""
) -> HTMLResponse:
    """Return article_content.html + OOB span to update the reading-time metadata.

    The story block lives inside that template, so its count has to be worked out here
    too: this render replaces the whole content block, and without it the block would
    disappear the moment an extraction finished.
    """
    related_count = 0
    if article.story_id is not None:
        # Only then is the setting worth a query: without a story there is no block to
        # draw either way.
        story_dedup = await db.scalar(
            select(UserSettings.story_dedup).where(UserSettings.user_id == user.id)
        )
        if story_dedup != DEDUP_OFF:
            related_count = await count_members(user.id, article.story_id, article.id, db)
    content_html = templates.env.get_template("app/partials/article_content.html").render(
        request=request, article=article, chat_available=False,
        related_count=related_count,
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
    # Set by app.js when this article's story is unfolded in the list: the members are
    # then rows of their own on screen and are read one by one, so this one keeps its
    # story to itself.
    story_unfolded: bool = Form(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await toggle_article_state(
        user, article_id, "is_read", db, close_story=not story_unfolded
    )
    if not article:
        return HTMLResponse("<p class='text-red-500 p-2 text-xs'>Article not found.</p>", status_code=404)
    return _read_response(request, article)


@router.post("/htmx/articles/{article_id}/set-read", response_class=HTMLResponse)
async def htmx_set_read(
    article_id: int,
    request: Request,
    state: bool = Query(True),
    story_unfolded: bool = Form(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    article = await update_article_state(
        user, article_id, ArticleStateUpdate(is_read=state), db,
        close_story=not story_unfolded,
    )
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
            set_={
                "dwell_seconds": UserArticleState.dwell_seconds + seconds,
                # Half a minute in front of an article the machine had closed on the
                # reader's behalf means they read it themselves after all, so the
                # machine mark goes away and the article can count as seen.
                "suppressed_at": case(
                    (UserArticleState.dwell_seconds + seconds >= ENGAGED_DWELL_SECONDS, null()),
                    else_=UserArticleState.suppressed_at,
                ),
                "suppressed_by": case(
                    (UserArticleState.dwell_seconds + seconds >= ENGAGED_DWELL_SECONDS, null()),
                    else_=UserArticleState.suppressed_by,
                ),
            },
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
        # Opening the link is the reader doing something with the article, so a
        # machine mark on it no longer stands (see the dwell handler above).
        state.suppressed_at = None
        state.suppressed_by = None
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
    return await _content_with_readtime_oob(request, article_resp, user, db)


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

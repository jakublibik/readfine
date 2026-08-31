"""AI pipeline: coordinates scoring → AI filters → summary inline after article is ready."""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.user import UserSettings

logger = logging.getLogger(__name__)


async def _get_scoring_job_status(article_id: int, user_id: int, db: AsyncSession) -> str | None:
    """Return the current status of the scoring job, or None if no job exists."""
    return await db.scalar(
        select(ArticleAiJob.status).where(
            ArticleAiJob.article_id == article_id,
            ArticleAiJob.user_id == user_id,
            ArticleAiJob.operation == "scoring",
        )
    )


async def _run_scoring_now(article: Article, user_id: int, db: AsyncSession) -> None:
    """Find and immediately process the pending scoring job for this article+user."""
    from app.services.ai_scoring_service import _execute_scoring_job

    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    job = await db.scalar(
        select(ArticleAiJob)
        .where(
            ArticleAiJob.article_id == article.id,
            ArticleAiJob.user_id == user_id,
            ArticleAiJob.operation == "scoring",
            ArticleAiJob.status == "pending",
        )
        .with_for_update(skip_locked=True)
    )
    if job is None or s is None:
        return
    await _execute_scoring_job(job, article, s, db, datetime.now(timezone.utc))


async def _run_ai_filters_now(article: Article, user_id: int, db: AsyncSession) -> None:
    """Apply AI filters for this article+user immediately."""
    from sqlalchemy.orm import selectinload

    from app.models.feed import UserFeed
    from app.models.filter import Filter
    from app.services.filter_service import FILTER_ORDER, _apply_ai_filters_for_state, is_ai_filter

    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id == article.id,
        )
    )
    if state is None or state.ai_filters_applied:
        return

    filters_result = await db.execute(
        select(Filter)
        .where(Filter.user_id == user_id, Filter.is_active == True)  # noqa: E712
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(*FILTER_ORDER)
    )
    ai_filters = [f for f in filters_result.scalars().all() if is_ai_filter(f)]

    uf = None
    if article.feed_id is not None:
        uf = await db.scalar(
            select(UserFeed).where(
                UserFeed.user_id == user_id,
                UserFeed.feed_id == article.feed_id,
            )
        )

    await _apply_ai_filters_for_state(state, article, uf, ai_filters, db)


async def _run_summary_now(article: Article, user_id: int, db: AsyncSession, pool=None) -> None:
    """Find and immediately process the pending summary job for this article+user.

    *pool* is passed on when this is reached from inside a batch, so the summary
    that follows a score shares the batch's clients rather than opening its own.
    """
    from app.services.ai_summary_service import _execute_summary_job

    job = await db.scalar(
        select(ArticleAiJob)
        .where(
            ArticleAiJob.article_id == article.id,
            ArticleAiJob.user_id == user_id,
            ArticleAiJob.operation == "summary",
            ArticleAiJob.status == "pending",
        )
        .with_for_update(skip_locked=True)
    )
    if job is None:
        return
    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None:
        return
    await _execute_summary_job(job, article, s, db, datetime.now(timezone.utc), pool)


async def run_article_pipeline(article: Article, user_id: int, db: AsyncSession) -> None:
    """Enqueue + immediately process scoring → AI filters → summary for one article+user."""
    from app.services.ai_scoring_service import enqueue_scoring_job

    # 1. scoring
    enqueued = await enqueue_scoring_job(article, user_id, db)
    if not enqueued:
        existing_status = await _get_scoring_job_status(article.id, user_id, db)
        if existing_status != "success":
            # ineligible, or a job already exists. A "pending" job is retried by
            # process_pending_scoring (transient errors back off + retry up to
            # _MAX_RETRIES). A "failed" job is terminal — permanent 4xx or
            # exhausted retries — and is intentionally not auto-requeued here to
            # avoid hammering a known-bad provider/model on every pipeline trigger.
            return
        # existing_status == "success" → score already set, continue to filters
    else:
        await _run_scoring_now(article, user_id, db)
        if await _get_scoring_job_status(article.id, user_id, db) != "success":
            return  # scoring failed

    # 2. AI filters
    await _run_ai_filters_now(article, user_id, db)

    # 3. auto-summary — only if enabled AND article is starred by this user
    await maybe_enqueue_starred_summary(article, user_id, db)

    logger.info("pipeline: article=%d user=%d done", article.id, user_id)


async def maybe_enqueue_starred_summary(
    article: Article, user_id: int, db: AsyncSession
) -> None:
    """Auto-summarize, but only when the user opted in *and* the article is starred.

    Shared with the save-by-URL path so a saved article follows exactly the same rule
    as a feed article: no policy of its own, no separate setting. A filter that stars
    it is what triggers the summary.
    """
    from app.services.ai_summary_service import enqueue_summary_job

    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if not (s and s.ai_summary_enabled_default):
        return
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id == article.id,
        )
    )
    if state and state.is_starred:
        if await enqueue_summary_job(article, user_id, db):
            await _run_summary_now(article, user_id, db)


async def run_pipeline_for_article_all_users(article: Article, db: AsyncSession) -> None:
    """Run the full pipeline for all users who have labeled this article (readable path).

    **No client pool here, on purpose** (decided 2026-08-30). The readable runner
    calls this once per article in a batch of 20, so the same user's client gets
    built once per article rather than once for the run, the way the scoring and
    summary batches do it (see ``ai_service.AiClientPool``).

    It was measured before it was left alone. Since the TLS context is shared,
    building a client costs about 0.1ms, so twenty of them is 2ms inside a batch
    that spends tens of seconds fetching twenty web pages. Gemini is the one
    exception at ~9ms, the rest of its constructor being its own object graph
    rather than TLS, which puts a readable run at roughly 0.18s of CPU.

    Getting a pool down here means threading it through this function,
    run_article_pipeline, _run_scoring_now and maybe_enqueue_starred_summary,
    none of which is a batch, one of which is shared with the save-by-URL path,
    and all of which sit in readable's call chain rather than AI's. Not worth
    0.17s for one provider. Worth revisiting if Gemini becomes the common case,
    or if the readable batch grows a lot.
    """
    from app.models.label import ArticleLabel

    user_ids = (await db.scalars(
        select(ArticleLabel.user_id)
        .where(ArticleLabel.article_id == article.id)
        .distinct()
    )).all()
    for uid in user_ids:
        await run_article_pipeline(article, uid, db)

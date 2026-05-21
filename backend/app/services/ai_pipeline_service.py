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
    from app.services.filter_service import _apply_ai_filters_for_state, is_ai_filter

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
        .order_by(Filter.position)
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


async def _run_summary_now(article: Article, user_id: int, db: AsyncSession) -> None:
    """Find and immediately process the pending summary job for this article+user."""
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
    await _execute_summary_job(job, article, s, db, datetime.now(timezone.utc))


async def run_article_pipeline(article: Article, user_id: int, db: AsyncSession) -> None:
    """Enqueue + immediately process scoring → AI filters → summary for one article+user."""
    from app.services.ai_scoring_service import enqueue_scoring_job
    from app.services.ai_summary_service import enqueue_summary_job

    # 1. scoring
    enqueued = await enqueue_scoring_job(article, user_id, db)
    if not enqueued:
        existing_status = await _get_scoring_job_status(article.id, user_id, db)
        if existing_status != "success":
            return  # ineligible or failed — batch runner handles retries
        # existing_status == "success" → score already set, continue to filters
    else:
        await _run_scoring_now(article, user_id, db)
        if await _get_scoring_job_status(article.id, user_id, db) != "success":
            return  # scoring failed

    # 2. AI filters
    await _run_ai_filters_now(article, user_id, db)

    # 3. auto-summary — only if user has auto-summarize enabled (global default or per-feed override)
    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s and s.ai_summary_enabled_default:
        if await enqueue_summary_job(article, user_id, db):
            await _run_summary_now(article, user_id, db)

    logger.info("pipeline: article=%d user=%d done", article.id, user_id)


async def run_pipeline_for_article_all_users(article: Article, db: AsyncSession) -> None:
    """Run the full pipeline for all users who have labeled this article (readable path)."""
    from app.models.label import ArticleLabel

    user_ids = (await db.scalars(
        select(ArticleLabel.user_id)
        .where(ArticleLabel.article_id == article.id)
        .distinct()
    )).all()
    for uid in user_ids:
        await run_article_pipeline(article, uid, db)

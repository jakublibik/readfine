"""AI summary/context pipeline: enqueue and process jobs."""
import logging
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.user import UserSettings
from app.services.ai_jobs import ai_enabled_globally, apply_job_failure, normalize_content

logger = logging.getLogger(__name__)

_BATCH_SIZE = 5
_MIN_CONTENT_CHARS = 1500
_DEFAULT_CONTENT_LIMIT = 20_000


async def enqueue_summary_job(article: Article, user_id: int, db: AsyncSession) -> bool:
    """
    Create a pending summary job for the given article + user if eligible.
    Returns True if a job was created.
    """
    if not await ai_enabled_globally(db):
        return False

    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None or not s.ai_quality_provider or not s.ai_quality_model:
        return False

    uf = None
    if article.feed_id is not None:
        from app.models.feed import UserFeed
        uf = await db.scalar(
            select(UserFeed).where(
                UserFeed.user_id == user_id,
                UserFeed.feed_id == article.feed_id,
            )
        )
        if uf is not None and uf.ai_summary_enabled is False:
            return False

    content_text = normalize_content(
        article.title, article.readable_content or article.content, _DEFAULT_CONTENT_LIMIT
    )
    if len(content_text) < _MIN_CONTENT_CHARS:
        return False

    result = await db.execute(
        pg_insert(ArticleAiJob).values(
            article_id=article.id,
            user_id=user_id,
            operation="summary",
            status="pending",
        ).on_conflict_do_nothing(
            index_elements=["article_id", "user_id", "operation"]
        )
    )
    return result.rowcount > 0


async def _execute_summary_job(
    job: ArticleAiJob, article: Article, s: UserSettings, db: AsyncSession, now: datetime
) -> None:
    """Process a single summary job — AI call + result write. Does not commit."""
    if s is None or not s.ai_quality_provider or not s.ai_quality_model:
        job.status = "skipped"
        job.processed_at = now
        return

    content_text = normalize_content(
        article.title, article.readable_content or article.content, s.ai_content_limit
    )
    if len(content_text) < _MIN_CONTENT_CHARS:
        job.status = "skipped"
        job.processed_at = now
        return

    from app.services.ai_service import get_ai_client, summarize_article
    client, provider, model = await get_ai_client(job.user_id, "quality", db)
    if client is None:
        job.status = "skipped"
        job.processed_at = now
        return

    try:
        result, in_tok, out_tok, truncated = await summarize_article(content_text, client, provider, model, custom_prompt=s.ai_summary_prompt)

        state = await db.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == job.user_id,
                UserArticleState.article_id == job.article_id,
            )
        )
        if state is None:
            state = UserArticleState(user_id=job.user_id, article_id=job.article_id)
            db.add(state)
        state.ai_summary = result
        # Always assigned, not only when true: regenerating clears a stale flag.
        state.ai_summary_truncated = truncated

        job.status = "success"
        job.processed_at = now
        job.error_message = None
        job.input_tokens = in_tok
        job.output_tokens = out_tok
        if s.last_ai_error:
            s.last_ai_error = None
            s.last_ai_error_at = None

    except Exception as exc:
        apply_job_failure(job, exc, now, operation="summary", settings=s)


async def _stored_summary(user_id: int, article_id: int, db: AsyncSession) -> tuple[str | None, bool]:
    """The summary written by a successful job, as (text, truncated)."""
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id == article_id,
        )
    )
    if state is None:
        return None, False
    return state.ai_summary, state.ai_summary_truncated


async def run_summary_on_demand(
    article: Article, user_id: int, db: AsyncSession
) -> tuple[str | None, bool, str | None]:
    """Enqueue + immediately process summary job.

    Returns (summary_text, truncated, error_message).
    On success: (text, bool, None). On failure: (None, False, error).
    On ineligible: (None, False, None).
    """
    await enqueue_summary_job(article, user_id, db)
    await db.flush()
    job = await db.scalar(
        select(ArticleAiJob).where(
            ArticleAiJob.article_id == article.id,
            ArticleAiJob.user_id == user_id,
            ArticleAiJob.operation == "summary",
        )
    )
    if job is None:
        return None, False, "Summary could not be started. Check that a quality AI model is configured."
    if job.status == "success":
        summary, truncated = await _stored_summary(user_id, article.id, db)
        return summary, truncated, None
    if job.status in ("failed", "skipped"):
        # On-demand: reset backoff so we retry immediately
        job.status = "pending"
        job.retry_count = 0
        job.next_retry_at = None
        await db.flush()
    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    now = datetime.now(timezone.utc)
    await _execute_summary_job(job, article, s, db, now)
    await db.commit()
    if job.status == "success":
        summary, truncated = await _stored_summary(user_id, article.id, db)
        return summary, truncated, None
    if job.status == "skipped":
        return None, False, "Article content is too short or AI model not available."
    # failed
    error = (job.error_message or "Unknown error")[:200]
    return None, False, error


async def process_pending_summaries(db: AsyncSession) -> int:
    """Process a batch of pending summary jobs. Returns number processed."""
    if not await ai_enabled_globally(db):
        return 0

    now = datetime.now(timezone.utc)
    jobs = (await db.execute(
        select(ArticleAiJob)
        .where(
            ArticleAiJob.operation == "summary",
            ArticleAiJob.status == "pending",
            and_(
                ArticleAiJob.next_retry_at.is_(None)
                | (ArticleAiJob.next_retry_at <= now)
            ),
        )
        .order_by(ArticleAiJob.id)
        .limit(_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )).scalars().all()

    if not jobs:
        return 0

    article_ids = list({j.article_id for j in jobs})
    user_ids = list({j.user_id for j in jobs})

    articles_map: dict[int, Article] = {
        a.id: a for a in (await db.scalars(select(Article).where(Article.id.in_(article_ids)))).all()
    }
    settings_map: dict[int, UserSettings] = {
        s.user_id: s for s in (await db.scalars(select(UserSettings).where(UserSettings.user_id.in_(user_ids)))).all()
    }

    processed = 0
    for job in jobs:
        article = articles_map.get(job.article_id)
        s = settings_map.get(job.user_id)

        if article is None or s is None:
            job.status = "skipped"
            job.processed_at = now
            processed += 1
            continue

        await _execute_summary_job(job, article, s, db, now)
        processed += 1

    await db.commit()
    logger.info("ai_summary: processed %d jobs", processed)
    return processed

"""AI scoring pipeline: enqueue and process article scoring jobs."""
import logging
from datetime import datetime, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.user import UserSettings
from app.services.ai_jobs import (
    ai_enabled_globally, apply_job_failure, clear_last_ai_error, normalize_content,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 30
_CONTENT_MAX_CHARS = 2000


def scoring_eligible(s: "UserSettings | None", uf: "UserFeed | None") -> bool:
    """Pure per-user/per-feed scoring eligibility (excludes the global ai_enabled
    kill-switch and the idempotency check, which the caller handles).

    Eligible only if:
    - user scoring enabled (ai_scoring_enabled_default on)
    - scoring not explicitly disabled for this feed (per-feed override)
    - preference text set
    - a fast AI slot is configured
    """
    if s is None or not s.ai_scoring_enabled_default:
        return False
    if uf is not None and uf.ai_scoring_enabled is False:
        return False
    if not s.ai_preference_text or not s.ai_preference_text.strip():
        return False
    if not s.ai_fast_provider or not s.ai_fast_model:
        return False
    return True


async def bulk_create_scoring_jobs(pairs: "list[tuple[int, int]]", db: AsyncSession) -> int:
    """Insert pending scoring jobs for the given (article_id, user_id) pairs in one
    statement. Idempotent — existing jobs are left untouched. Returns rows inserted.

    Callers are responsible for eligibility filtering; this only writes rows.
    """
    if not pairs:
        return 0
    rows = [
        {"article_id": aid, "user_id": uid, "operation": "scoring", "status": "pending"}
        for aid, uid in pairs
    ]
    result = await db.execute(
        pg_insert(ArticleAiJob)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["article_id", "user_id", "operation"])
    )
    return result.rowcount or 0


async def enqueue_scoring_job(article: Article, user_id: int, db: AsyncSession) -> bool:
    """
    Create a pending scoring job for the given article + user if eligible.
    Returns True if a job was created.

    Skipped if:
    - admin ai_enabled kill-switch is off
    - user scoring not enabled (ai_scoring_enabled_default off, per-feed not overridden to on)
    - no preference text set
    - no AI client configured for fast slot
    - job already exists for this article/user/operation
    """
    if not await ai_enabled_globally(db):
        return False

    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    # Early-exit on the cheapest disqualifier before loading the per-feed override.
    if s is None or not s.ai_scoring_enabled_default:
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

    if not scoring_eligible(s, uf):
        return False

    # Idempotent: skip if any job already exists for this article/user/operation
    existing = await db.scalar(
        select(ArticleAiJob.id).where(
            ArticleAiJob.article_id == article.id,
            ArticleAiJob.user_id == user_id,
            ArticleAiJob.operation == "scoring",
        )
    )
    if existing is not None:
        return False

    result = await db.execute(
        pg_insert(ArticleAiJob).values(
            article_id=article.id,
            user_id=user_id,
            operation="scoring",
            status="pending",
        ).on_conflict_do_nothing(
            index_elements=["article_id", "user_id", "operation"]
        )
    )
    return result.rowcount > 0


async def _execute_scoring_job(
    job: ArticleAiJob, article: Article, s: UserSettings, db: AsyncSession, now: datetime
) -> None:
    """Process a single scoring job — AI call + result write. Does not commit."""
    if not s.ai_preference_text or not s.ai_fast_provider or not s.ai_fast_model:
        job.status = "skipped"
        job.processed_at = now
        return

    content_text = normalize_content(
        article.title, article.readable_content or article.content, _CONTENT_MAX_CHARS
    )

    from app.services.ai_service import get_ai_client, score_article
    client, provider, model = await get_ai_client(job.user_id, "fast", db)
    if client is None:
        job.status = "skipped"
        job.processed_at = now
        return

    # Recorded before the call, so a failed attempt also says which model failed.
    job.provider = provider
    job.model = model

    try:
        score, in_tok, out_tok = await score_article(content_text, s.ai_preference_text, client, provider, model)

        job.input_tokens = in_tok
        job.output_tokens = out_tok

        state = await db.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == job.user_id,
                UserArticleState.article_id == job.article_id,
            )
        )
        if state is None:
            state = UserArticleState(user_id=job.user_id, article_id=job.article_id)
            db.add(state)
        state.ai_score = score
        state.ai_filters_applied = False

        job.status = "success"
        job.processed_at = now
        job.error_message = None
        if s.last_ai_error:
            clear_last_ai_error(s)

    except Exception as exc:
        apply_job_failure(job, exc, now, operation="scoring", settings=s)


async def process_pending_scoring(db: AsyncSession) -> int:
    """
    Process a batch of pending scoring jobs, then run AI filters + summary inline.
    Returns number of jobs processed.
    """
    if not await ai_enabled_globally(db):
        return 0

    now = datetime.now(timezone.utc)
    jobs_result = await db.execute(
        select(ArticleAiJob)
        .where(
            ArticleAiJob.operation == "scoring",
            ArticleAiJob.status == "pending",
            # skip jobs whose article has been retention-trimmed (body gone)
            ~select(Article.id)
            .where(Article.id == ArticleAiJob.article_id, Article.trimmed_at.isnot(None))
            .exists(),
            and_(
                ArticleAiJob.next_retry_at.is_(None)
                | (ArticleAiJob.next_retry_at <= now)
            ),
        )
        .order_by(ArticleAiJob.id)
        .limit(_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    jobs = jobs_result.scalars().all()
    if not jobs:
        return 0

    # Pre-load articles, settings, and states to avoid N+1 queries
    article_ids = list({j.article_id for j in jobs})
    user_ids = list({j.user_id for j in jobs})

    articles_map: dict[int, Article] = {
        a.id: a for a in (await db.scalars(select(Article).where(Article.id.in_(article_ids)))).all()
    }
    settings_map: dict[int, UserSettings] = {
        s.user_id: s for s in (await db.scalars(select(UserSettings).where(UserSettings.user_id.in_(user_ids)))).all()
    }
    states_map: dict[tuple[int, int], UserArticleState] = {
        (st.user_id, st.article_id): st
        for st in (await db.scalars(
            select(UserArticleState).where(
                UserArticleState.article_id.in_(article_ids),
                UserArticleState.user_id.in_(user_ids),
            )
        )).all()
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

        await _execute_scoring_job(job, article, s, db, now)

        if job.status == "success":
            from app.services.ai_pipeline_service import _run_ai_filters_now, _run_summary_now
            from app.services.ai_summary_service import enqueue_summary_job
            await _run_ai_filters_now(article, job.user_id, db)
            if s.ai_summary_enabled_default:
                state = states_map.get((job.user_id, job.article_id))
                if state and state.is_starred:
                    if await enqueue_summary_job(article, job.user_id, db):
                        await _run_summary_now(article, job.user_id, db)
            logger.info("pipeline: article=%d user=%d done (scoring path)", article.id, job.user_id)

        processed += 1

    await db.commit()
    logger.info("ai_scoring: processed %d jobs", processed)
    return processed

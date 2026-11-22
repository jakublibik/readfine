"""AI summary/context pipeline: enqueue and process jobs."""
import logging
from datetime import datetime, timezone

from sqlalchemy import and_, func, not_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.user import UserSettings
from app.services.ai_jobs import (
    ON_DEMAND_MIN_CHARS, ai_enabled_globally, ai_work_held_back, apply_job_failure,
    clear_last_ai_error, normalize_text,
)

logger = logging.getLogger(__name__)

_BATCH_SIZE = 5

# job_params of a summary queued by a star while the full text was still on its way.
# The job itself is the waiting: whichever path ends the extraction, the article
# leaves 'pending' and the job becomes runnable, so none of them has to create it.
AWAITS_READABLE = {"awaits_readable": True}


def awaits_readable(article: Article) -> bool:
    """Is an extraction still coming? The readable batch passes by a pending article
    without an address or trimmed to a stub, so there is nothing to wait for there."""
    return bool(article.readable_status == "pending" and article.url
                and article.trimmed_at is None)


def summary_runnable_clause():
    """SQL: the summary job is not waiting for an extraction (see ``awaits_readable``)."""
    extracting = (
        select(Article.id)
        .where(Article.id == ArticleAiJob.article_id,
               Article.readable_status == "pending",
               Article.url != "",
               Article.trimmed_at.is_(None))
        .correlate(ArticleAiJob)
        .exists()
    )
    waiting = func.coalesce(ArticleAiJob.job_params.contains(AWAITS_READABLE), False)
    return not_(and_(waiting, extracting))


async def enqueue_summary_job(
    article: Article, user_id: int, db: AsyncSession, *, min_chars: int | None = None,
    wait_for_readable: bool = False,
) -> bool:
    """
    Create a pending summary job for the given article + user if eligible.
    Returns True if a job was created.

    *min_chars* defaults to the user's minimum length, which is what gates the
    automatic pipeline. On-demand runs pass ``ON_DEMAND_MIN_CHARS`` instead: the
    setting is there to stop automation spending on articles too short to be
    worth it, not to overrule someone pressing the button on one of them.

    *wait_for_readable* queues the job without measuring it: there is only the
    feed's excerpt to measure yet. ``release_summary_job`` measures the full text.
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

    if not wait_for_readable and not _long_enough(article, s, uf, min_chars):
        return False

    if await ai_work_held_back(user_id, db):
        return False

    values = dict(article_id=article.id, user_id=user_id, operation="summary", status="pending")
    if wait_for_readable:
        values["job_params"] = AWAITS_READABLE
    result = await db.execute(
        pg_insert(ArticleAiJob).values(**values).on_conflict_do_nothing(
            index_elements=["article_id", "user_id", "operation"]
        )
    )
    return result.rowcount > 0


def _long_enough(article: Article, s: UserSettings, uf, min_chars: int | None = None) -> bool:
    # Measured on the full text, not the copy truncated to the content limit, so
    # the answer does not change with a limit that has nothing to do with it.
    # Nothing is sent to a model here, so there is nothing to truncate either.
    threshold = s.ai_min_content_chars if min_chars is None else min_chars
    # A feed the reader translates: the summary, written in their language, is the
    # only way they read the article at all, so a short one is no reason to skip it.
    # Only the floor of the Summarize button is left.
    if uf is not None and uf.translate and s.ai_translation_language:
        threshold = min(threshold, ON_DEMAND_MIN_CHARS)
    full_text = normalize_text(article.title, article.readable_content or article.content)
    return len(full_text) >= threshold


async def release_summary_job(
    job: ArticleAiJob, article: Article, s: UserSettings, db: AsyncSession
) -> bool:
    """May this job run now? Settles one that waited for the full text.

    What the star could not decide is decided here, once: the length, and whether the
    star and the feed's switch still stand, as the wait can take hours. A job that
    does not qualify is deleted, as if never queued, so the Summarize button works.
    """
    if job.job_params != AWAITS_READABLE:
        return True
    if awaits_readable(article) or not await ai_enabled_globally(db):
        return False

    from app.models.feed import UserFeed

    starred = await db.scalar(
        select(UserArticleState.is_starred).where(
            UserArticleState.user_id == job.user_id,
            UserArticleState.article_id == job.article_id,
        )
    )
    uf = await db.scalar(
        select(UserFeed).where(
            UserFeed.user_id == job.user_id, UserFeed.feed_id == article.feed_id,
        )
    )
    if (not starred or (uf is not None and uf.ai_summary_enabled is False)
            or not _long_enough(article, s, uf)):
        await db.delete(job)
        return False
    job.job_params = None
    return True


async def _execute_summary_job(
    job: ArticleAiJob, article: Article, s: UserSettings, db: AsyncSession, now: datetime,
    pool=None,
) -> bool:
    """Process a single summary job — AI call + result write. Does not commit.

    *pool* is the batch's client pool when this runs as part of one; see
    :func:`ai_scoring_service._execute_scoring_job`, which also explains the
    return value.
    """
    if s is None or not s.ai_quality_provider or not s.ai_quality_model:
        job.status = "skipped"
        job.processed_at = now
        return False

    # No length check here on purpose. Whether an article is worth summarizing is
    # settled when the job is created, by whoever created it: the pipeline against
    # the user's minimum, a button press against the on-demand floor. Re-deciding
    # it at execution time would have to guess which of the two this job came from
    # (and would quietly drop a hand-requested summary that came back for a retry),
    # and it would let a settings change cancel work already queued, which is not
    # what "applies from now on" promises.
    content_text = normalize_text(
        article.title, article.readable_content or article.content
    )[:s.ai_content_limit]

    from app.services.ai_service import AiCallTimeout, ai_client, summarize_article
    async with ai_client(job.user_id, "quality", db, pool) as (client, provider, model):
        if client is None:
            job.status = "skipped"
            job.processed_at = now
            return False

        # Recorded before the call, so a failed attempt also says which model failed.
        job.provider = provider
        job.model = model

        try:
            from app.services.translation_service import output_language
            answer = await summarize_article(
                content_text, client, provider, model, custom_prompt=s.ai_summary_prompt,
                language=await output_language(s, article.feed_id, db),
            )

            state = await db.scalar(
                select(UserArticleState).where(
                    UserArticleState.user_id == job.user_id,
                    UserArticleState.article_id == job.article_id,
                )
            )
            if state is None:
                state = UserArticleState(user_id=job.user_id, article_id=job.article_id)
                db.add(state)
            state.ai_summary = answer.text
            # Always assigned, not only when true: regenerating clears a stale flag.
            state.ai_summary_truncated = answer.truncated

            job.status = "success"
            job.processed_at = now
            job.error_message = None
            job.input_tokens = answer.input_tokens
            job.output_tokens = answer.output_tokens
            if s.last_ai_error:
                clear_last_ai_error(s)

        except Exception as exc:
            apply_job_failure(job, exc, now, operation="summary", settings=s)
            return isinstance(exc, AiCallTimeout)
    return False


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
    await enqueue_summary_job(article, user_id, db, min_chars=ON_DEMAND_MIN_CHARS)
    await db.flush()
    job = await db.scalar(
        select(ArticleAiJob).where(
            ArticleAiJob.article_id == article.id,
            ArticleAiJob.user_id == user_id,
            ArticleAiJob.operation == "summary",
        )
    )
    if job is None:
        return None, False, "Summary could not be started. Check that a main AI model is configured."
    if job.status == "success":
        summary, truncated = await _stored_summary(user_id, article.id, db)
        return summary, truncated, None
    # The button does not wait for the full text, and neither does a retry of it.
    job.job_params = None
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
        # Length cannot land here: the guard and enqueue both measure against the
        # on-demand floor, and execution does not measure at all. What is left is
        # the model disappearing between the guard and the run.
        return None, False, "Summary was skipped. Check that a main AI model is configured."
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
            summary_runnable_clause(),
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

    from app.services.ai_service import AiClientPool

    processed = 0
    # Same reason as in process_pending_scoring: one slow account must not make
    # the rest of the batch wait a full budget per job.
    timed_out: set[int] = set()
    async with AiClientPool() as pool:
        for job in jobs:
            if job.user_id in timed_out:
                continue
            article = articles_map.get(job.article_id)
            s = settings_map.get(job.user_id)

            if article is None or s is None:
                job.status = "skipped"
                job.processed_at = now
                processed += 1
                continue

            if not await release_summary_job(job, article, s, db):
                processed += 1
                continue
            if await _execute_summary_job(job, article, s, db, now, pool):
                timed_out.add(job.user_id)
            processed += 1

    await db.commit()
    logger.info("ai_summary: processed %d jobs", processed)
    return processed

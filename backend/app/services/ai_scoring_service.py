"""AI scoring pipeline: enqueue and process article scoring jobs."""
import html as html_module
import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.settings import AppSettings
from app.models.user import UserSettings

logger = logging.getLogger(__name__)

_BATCH_SIZE = 30
_MAX_RETRIES = 3
_BACKOFF_MINUTES = [5, 30, 120]
_CONTENT_MAX_CHARS = 2000

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_content(title: str, content: str | None) -> str:
    """Strip HTML, collapse whitespace, prepend title, truncate to _CONTENT_MAX_CHARS."""
    import nh3
    raw = content or ""
    plain = nh3.clean(raw, tags=set())
    plain = html_module.unescape(plain)
    plain = _WHITESPACE_RE.sub(" ", plain).strip()
    combined = f"{title}\n\n{plain}" if plain else title
    return combined[:_CONTENT_MAX_CHARS]


async def _ai_enabled_globally(db: AsyncSession) -> bool:
    row = await db.scalar(select(AppSettings.ai_enabled).where(AppSettings.id == 1))
    return bool(row)


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
    if not await _ai_enabled_globally(db):
        return False

    s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if s is None or not s.ai_scoring_enabled_default:
        return False

    # Per-feed override: check if scoring is explicitly disabled for this feed
    if article.feed_id is not None:
        from app.models.feed import UserFeed
        uf = await db.scalar(
            select(UserFeed).where(
                UserFeed.user_id == user_id,
                UserFeed.feed_id == article.feed_id,
            )
        )
        if uf is not None and uf.ai_scoring_enabled is False:
            return False

    if not s.ai_preference_text or not s.ai_preference_text.strip():
        return False

    if not s.ai_fast_provider or not s.ai_fast_model:
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

    content_text = _normalize_content(
        article.title,
        article.readable_content or article.content,
    )

    from app.services.ai_service import get_ai_client, score_article
    client, provider, model = await get_ai_client(job.user_id, "fast", db)
    if client is None:
        job.status = "skipped"
        job.processed_at = now
        return

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
            s.last_ai_error = None
            s.last_ai_error_at = None

    except Exception as exc:
        msg = str(exc)[:300]
        http_status = _extract_http_status(exc)
        retries = job.retry_count + 1
        job.retry_count = retries

        if http_status is not None and 400 <= http_status < 500 and http_status != 429:
            job.status = "failed"
            job.processed_at = now
        elif retries >= _MAX_RETRIES:
            job.status = "failed"
            job.processed_at = now
        else:
            delay = _BACKOFF_MINUTES[min(retries - 1, len(_BACKOFF_MINUTES) - 1)]
            job.next_retry_at = now + timedelta(minutes=delay)

        job.error_message = msg
        logger.warning("AI scoring failed job=%d article=%d user=%d: %s", job.id, job.article_id, job.user_id, msg)

        if s:
            s.last_ai_error = f"Scoring error: {msg}"
            s.last_ai_error_at = now


async def process_pending_scoring(db: AsyncSession) -> int:
    """
    Process a batch of pending scoring jobs, then run AI filters + summary inline.
    Returns number of jobs processed.
    """
    if not await _ai_enabled_globally(db):
        return 0

    now = datetime.now(timezone.utc)
    jobs_result = await db.execute(
        select(ArticleAiJob)
        .where(
            ArticleAiJob.operation == "scoring",
            ArticleAiJob.status == "pending",
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


def _extract_http_status(exc: Exception) -> int | None:
    """Best-effort extraction of HTTP status code from provider exceptions."""
    for attr in ("status_code", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    msg = str(exc)
    m = re.search(r"\b([45]\d{2})\b", msg)
    return int(m.group(1)) if m else None

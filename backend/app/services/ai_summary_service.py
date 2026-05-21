"""AI summary/context pipeline: enqueue and process jobs."""
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

_BATCH_SIZE = 5
_MAX_RETRIES = 3
_BACKOFF_MINUTES = [5, 30, 120]
_MIN_CONTENT_CHARS = 1500
_CONTENT_MAX_CHARS = 12_000

_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_content(title: str, content: str | None) -> str:
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


async def enqueue_summary_job(article: Article, user_id: int, db: AsyncSession) -> bool:
    """
    Create a pending summary job for the given article + user if eligible.
    Returns True if a job was created.
    """
    if not await _ai_enabled_globally(db):
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

    content_text = _normalize_content(article.title, article.readable_content or article.content)
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

    content_text = _normalize_content(article.title, article.readable_content or article.content)
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
        result = await summarize_article(content_text, client, provider, model, custom_prompt=s.ai_summary_prompt)

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

        job.status = "success"
        job.processed_at = now
        job.error_message = None
        if s.last_ai_error:
            s.last_ai_error = None
            s.last_ai_error_at = None

    except Exception as exc:
        msg = str(exc)[:300]
        retries = job.retry_count + 1
        job.retry_count = retries
        http_status = _extract_http_status(exc)

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
        logger.warning("AI summary failed job=%d article=%d user=%d: %s", job.id, job.article_id, job.user_id, msg)
        s.last_ai_error = f"Summary error: {msg}"
        s.last_ai_error_at = now


async def run_summary_on_demand(
    article: Article, user_id: int, db: AsyncSession
) -> tuple[str | None, str | None]:
    """Enqueue + immediately process summary job.

    Returns (summary_text, error_message).
    On success: (text, None). On failure: (None, error). On ineligible: (None, None).
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
        return None, "Summary could not be started. Check that a quality AI model is configured."
    if job.status == "success":
        state = await db.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id == article.id,
            )
        )
        return (state.ai_summary if state else None), None
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
        state = await db.scalar(
            select(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id == article.id,
            )
        )
        return (state.ai_summary if state else None), None
    if job.status == "skipped":
        return None, "Article content is too short or AI model not available."
    # failed
    error = (job.error_message or "Unknown error")[:200]
    return None, error


async def process_pending_summaries(db: AsyncSession) -> int:
    """Process a batch of pending summary jobs. Returns number processed."""
    if not await _ai_enabled_globally(db):
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


def _extract_http_status(exc: Exception) -> int | None:
    for attr in ("status_code", "http_status", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    msg = str(exc)
    m = re.search(r"\b([45]\d{2})\b", msg)
    return int(m.group(1)) if m else None

"""AI translation of article titles: enqueue after a fetch, translate in batches.

A reader picks one target language (``UserSettings.ai_translation_language``) and
turns translation on per feed (``UserFeed.translate``). New articles of such a feed
get a pending ``ArticleAiJob`` with operation ``translation`` inside the fetch
transaction, which only writes rows. The model call happens here, after the fetch
has committed: a provider outage must never hold up or break a fetch.

Jobs run in batches of one reader's titles from one feed, sent as JSON with ids and
checked one by one on the way back. A batch the model cannot answer as a whole
(unparseable, cut off, refused) is halved and retried, so one headline the model
balks at does not hold back the others. Good answers are stored even when others
in the same reply are not.

Translations are never shared between readers: each uses their own key and model,
and the result lives in ``article_translations`` keyed by reader, article and
language.
"""
import asyncio
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import AiUsageLog, Article, ArticleAiJob, ArticleTranslation
from app.models.feed import UserFeed
from app.models.user import UserSettings
from app.services.ai_jobs import (
    ai_enabled_globally, ai_work_held_back, apply_job_failure, clear_last_ai_error,
)

logger = logging.getLogger(__name__)

# Code -> English name. The name goes into the prompt, so the reader picks from this
# list and never types one. English names because the prompt is in English.
LANGUAGES: dict[str, str] = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "et": "Estonian",
    "fi": "Finnish",
    "fr": "French",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sr": "Serbian",
    "sv": "Swedish",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese (Simplified)",
}

SLOTS = ("fast", "quality")

# Titles per model call. The quality check ran 120 headlines in batches of 20 with
# nothing missing or cut off, and a failed batch costs at most this many retries.
BATCH_SIZE = 20
# Batches per run, so one run cannot hold the lock for a whole backlog; the next
# run (a kick or the minute job) carries on.
_MAX_BATCHES_PER_RUN = 25

# A translation this much longer than its original is not a headline any more (a
# model that explained instead of translating). Latin script from CJK runs about
# three times longer, so the factor leaves room for that.
_MAX_LENGTH_FACTOR = 5
_MIN_LENGTH_CAP = 300
_TITLE_MAX = 1000  # the column

_WHITESPACE_RE = re.compile(r"\s+")


def language_name(code: str | None) -> str | None:
    return LANGUAGES.get(code) if code else None


def translation_slot(s: UserSettings) -> str:
    return s.ai_translation_slot if s.ai_translation_slot in SLOTS else "fast"


# ── enqueue ───────────────────────────────────────────────────────────────────

# session.info key: the fetch that enqueued jobs kicks a run once it has committed.
ENQUEUED_FLAG = "translation_jobs_enqueued"


async def enqueue_translation_jobs(feed_id: int, articles: list[Article], db: AsyncSession) -> int:
    """Pending translation jobs for *articles* of *feed_id*, one per reader who
    translates the feed. Called from both fetch paths after the flush, inside the
    fetch transaction: it only writes rows. Returns the number of jobs created.

    Sets ``db.info[ENQUEUED_FLAG]`` when it created any, which tells the fetch to
    kick a run after its commit.
    """
    if not articles:
        return 0
    user_ids = (await db.scalars(
        select(UserFeed.user_id)
        .join(UserSettings, UserSettings.user_id == UserFeed.user_id)
        .where(
            UserFeed.feed_id == feed_id,
            UserFeed.translate.is_(True),
            UserSettings.ai_translation_language.is_not(None),
        )
    )).all()
    if not user_ids or not await ai_enabled_globally(db):
        return 0
    eligible = [uid for uid in user_ids if not await ai_work_held_back(uid, db)]
    created = await bulk_create_translation_jobs(
        [(a.id, uid) for uid in eligible for a in articles], db
    )
    if created:
        db.info[ENQUEUED_FLAG] = True
    return created


async def bulk_create_translation_jobs(pairs: list[tuple[int, int]], db: AsyncSession) -> int:
    """Insert pending jobs for (article_id, user_id) pairs. Existing jobs are left
    alone. Returns rows inserted."""
    if not pairs:
        return 0
    result = await db.execute(
        pg_insert(ArticleAiJob)
        .values([
            {"article_id": aid, "user_id": uid, "operation": "translation", "status": "pending"}
            for aid, uid in pairs
        ])
        .on_conflict_do_nothing(index_elements=["article_id", "user_id", "operation"])
    )
    return result.rowcount or 0


# ── parsing ───────────────────────────────────────────────────────────────────

def parse_translations(raw: str, originals: dict[int, str]) -> dict[int, str]:
    """The usable translations in a model reply, by article id.

    Tolerates a code fence or a sentence around the array. Skips ids that were not
    asked for and answers that are empty or too long to be a headline. Anything
    unparseable gives an empty dict: the caller treats the whole batch as
    unanswered.
    """
    text = (raw or "").strip()
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    if not isinstance(data, list):
        return {}

    out: dict[int, str] = {}
    for obj in data:
        if not isinstance(obj, dict):
            continue
        try:
            aid = int(obj.get("id"))
        except (TypeError, ValueError):
            continue
        title = obj.get("title")
        if aid not in originals or not isinstance(title, str):
            continue
        title = _WHITESPACE_RE.sub(" ", title).strip()
        cap = min(_TITLE_MAX, max(_MIN_LENGTH_CAP, _MAX_LENGTH_FACTOR * len(originals[aid])))
        if not title or len(title) > cap:
            continue
        out[aid] = title
    return out


# ── processing ────────────────────────────────────────────────────────────────

class _BatchRun:
    """One reader's run over one batch: the client, the settings and the log."""

    def __init__(self, s: UserSettings, client, provider: str, model: str,
                 slot: str, language: str, db: AsyncSession, now: datetime):
        self.s = s
        self.client = client
        self.provider = provider
        self.model = model
        self.slot = slot
        self.language = language
        self.db = db
        self.now = now
        self.timed_out = False

    async def translate(self, jobs: list[ArticleAiJob], titles: dict[int, str]) -> None:
        """Translate *jobs*, halving on an answer that does not cover them all."""
        from app.services.ai_service import AiCallTimeout, ProviderEmptyResponse, translate_titles

        if self.timed_out:
            return
        for job in jobs:
            job.provider = self.provider
            job.model = self.model

        items = [{"id": j.article_id, "title": titles[j.article_id]} for j in jobs]
        try:
            answer = await translate_titles(
                items, LANGUAGES[self.language], self.client, self.provider, self.model)
        except ProviderEmptyResponse as exc:
            # The model said nothing, which a refusal of one headline can look like.
            # Worth splitting; anything else from the provider (auth, rate limit,
            # outage) fails every half the same way, so it is not.
            if len(jobs) > 1:
                await self._split(jobs, titles)
            else:
                self._fail(jobs, exc, banner=False)
            return
        except Exception as exc:
            self._fail(jobs, exc)
            if isinstance(exc, AiCallTimeout):
                self.timed_out = True
            return

        self.db.add(AiUsageLog(
            user_id=self.s.user_id, operation="translation", model_slot=self.slot,
            model=self.model, provider=self.provider,
            input_tokens=answer.input_tokens, output_tokens=answer.output_tokens,
        ))
        got = {} if answer.truncated else parse_translations(
            answer.text, {j.article_id: titles[j.article_id] for j in jobs})

        if got:
            stmt = pg_insert(ArticleTranslation).values([
                {"user_id": self.s.user_id, "article_id": aid, "language": self.language,
                 "title": title, "model": self.model}
                for aid, title in got.items()
            ])
            await self.db.execute(stmt.on_conflict_do_update(
                index_elements=["user_id", "article_id", "language"],
                set_={"title": stmt.excluded.title, "model": stmt.excluded.model,
                      "created_at": self.now},
            ))
            for job in jobs:
                if job.article_id in got:
                    job.status = "success"
                    job.processed_at = self.now
                    job.error_message = None
            if self.s.last_ai_error:
                clear_last_ai_error(self.s)

        missing = [j for j in jobs if j.article_id not in got]
        if not missing:
            return
        if len(jobs) == 1:
            reason = "reply was cut off" if answer.truncated else "no usable translation in the reply"
            self._fail(missing, ValueError(reason), banner=False)
        else:
            # Strictly smaller than this batch either way, so the recursion ends.
            await self._split(missing, titles)

    async def _split(self, jobs: list[ArticleAiJob], titles: dict[int, str]) -> None:
        if len(jobs) == 1:
            await self.translate(jobs, titles)
            return
        mid = len(jobs) // 2
        await self.translate(jobs[:mid], titles)
        await self.translate(jobs[mid:], titles)

    def _fail(self, jobs: list[ArticleAiJob], exc: Exception, *, banner: bool = True) -> None:
        """*banner* off for a headline the model would not translate: that is about
        the headline, not the reader's key or provider, and the reader cannot act on
        it. Provider errors (auth, rate limit, outage, timeout) still show."""
        settings = self.s if banner else None
        for job in jobs:
            apply_job_failure(job, exc, self.now, operation="translation", settings=settings)


def _skip(jobs: list[ArticleAiJob], now: datetime) -> None:
    for job in jobs:
        job.status = "skipped"
        job.processed_at = now


def _due_jobs(now: datetime):
    return (
        select(ArticleAiJob, Article.feed_id, Article.title)
        .join(Article, Article.id == ArticleAiJob.article_id)
        .where(
            ArticleAiJob.operation == "translation",
            ArticleAiJob.status == "pending",
            ArticleAiJob.next_retry_at.is_(None) | (ArticleAiJob.next_retry_at <= now),
        )
        .order_by(ArticleAiJob.id)
        .with_for_update(of=ArticleAiJob, skip_locked=True)
    )


async def process_pending_translations(db: AsyncSession) -> int:
    """Work through pending translation jobs, one (reader, feed) batch at a time,
    committing after each so finished titles show up while the rest still run.
    Returns the number of jobs that reached a final state or a backoff.
    """
    from app.services.ai_service import AiClientPool, ai_client

    if not await ai_enabled_globally(db):
        return 0

    processed = 0
    # Readers whose model ran out of time in this run: their other jobs wait for
    # the next one rather than costing the full budget again, batch after batch.
    timed_out: set[int] = set()
    async with AiClientPool() as pool:
        for _ in range(_MAX_BATCHES_PER_RUN):
            now = datetime.now(timezone.utc)
            q = _due_jobs(now)
            if timed_out:
                q = q.where(ArticleAiJob.user_id.not_in(timed_out))
            first = (await db.execute(q.limit(1))).first()
            if first is None:
                break
            user_id, feed_id = first[0].user_id, first[1]
            rows = (await db.execute(
                q.where(
                    ArticleAiJob.user_id == user_id,
                    Article.feed_id.is_not_distinct_from(feed_id),
                ).limit(BATCH_SIZE)
            )).all()
            jobs = [r[0] for r in rows]
            titles = {r[0].article_id: r[2] for r in rows}

            s = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
            translating = feed_id is not None and await db.scalar(
                select(UserFeed.translate).where(
                    UserFeed.user_id == user_id, UserFeed.feed_id == feed_id)
            )
            if (
                s is None or s.ai_translation_language not in LANGUAGES
                or not translating or await ai_work_held_back(user_id, db)
            ):
                # Turned off (or unsubscribed) since the job was queued. Turning it
                # back on queues the recent articles again.
                _skip(jobs, now)
            else:
                slot = translation_slot(s)
                async with ai_client(user_id, slot, db, pool) as (client, provider, model):
                    if client is None:
                        # No key or no model in the slot: settings say so.
                        _skip(jobs, now)
                    else:
                        run = _BatchRun(s, client, provider, model, slot,
                                        s.ai_translation_language, db, now)
                        await run.translate(jobs, titles)
                        if run.timed_out:
                            timed_out.add(user_id)
            processed += len(jobs)
            await db.commit()

    if processed:
        logger.info("translation: processed %d jobs", processed)
    return processed


# ── running ───────────────────────────────────────────────────────────────────

# One run at a time in this process: the scheduler's minute job and the kicks after
# each fetch share it. A kick that arrives during a run asks for one more pass
# instead of starting a second runner next to it.
_run_lock = asyncio.Lock()
_rerun = False


async def run_translations() -> None:
    """Process pending translations with a fresh session, unless a run is already
    going (it then makes one more pass)."""
    global _rerun
    import app.database as database

    if database.async_session_factory is None:
        return
    if _run_lock.locked():
        _rerun = True
        return
    async with _run_lock:
        while True:
            _rerun = False
            async with database.async_session_factory() as session:
                await process_pending_translations(session)
            if not _rerun:
                break


def kick_translations(db: AsyncSession) -> None:
    """After a fetch commit: start a run if this session enqueued jobs."""
    if db.info.pop(ENQUEUED_FLAG, False) is not True:
        return
    from app.utils.background import spawn_background
    spawn_background(run_translations(), name="translations")

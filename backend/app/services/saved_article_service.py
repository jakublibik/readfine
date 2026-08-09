"""Save an article by URL: import, background extraction, post-processing.

An article saved this way has no feed. ``UserArticleState.saved_at`` carries its
visibility (the Saved view), its access (see ``article_access_predicate``) and its
exemption from retention purge, so it never has to borrow a star to stay alive.
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.user import User
from app.services.readable_service import (
    ReadableResult,
    apply_readable_result,
    extract_readable_with_title,
    title_from_url,
)
from app.utils.parsing import normalize_url

logger = logging.getLogger(__name__)

# Keep the batch worker off a row the import task is about to handle. The scheduler
# runs process_pending_readable every minute and takes no row locks, so a row inserted
# as 'pending' with a NULL retry time is eligible immediately — both would extract it,
# and both would post-process it. This is not user-visible latency: the import task
# starts extracting straight away. It only bounds recovery when the process dies
# mid-import, and it is ~8x the 15s extraction timeout, which leaves room for a busy
# executor.
_IMPORT_BUFFER_MIN = 2

# Below this, feed-supplied content is treated as "nothing to show" and a manual save
# is allowed to re-extract a shared article. Above it, the feed already gives readers
# something and re-extraction risks replacing that with an error banner for everyone.
_USABLE_CONTENT_CHARS = 400

# Width of articles.url and articles.guid. An address past it is refused rather than
# stored cut down: the fetch would still use the whole thing and succeed, leaving a
# row whose stored address is a broken link, which is what "Open original" offers and
# what Retry re-fetches.
_URL_MAX = 2048


def _buffer_until() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=_IMPORT_BUFFER_MIN)


async def _upsert_saved_state(article_id: int, user_id: int, db: AsyncSession) -> None:
    """Mark the article saved for this user, creating the state row if needed."""
    now = datetime.now(timezone.utc)
    await db.execute(
        pg_insert(UserArticleState)
        .values(user_id=user_id, article_id=article_id, saved_at=now, is_read=False)
        .on_conflict_do_update(
            index_elements=["user_id", "article_id"],
            set_={"saved_at": now},
        )
    )


def _has_usable_content(article: Article) -> bool:
    if article.readable_content and article.readable_content.strip():
        return True
    return bool(article.content and len(article.content.strip()) > _USABLE_CONTENT_CHARS)


async def save_article_by_url(
    url: str, user: User, db: AsyncSession
) -> tuple[Article, bool]:
    """Save a pasted URL for this user. Returns (article, already_known).

    Raises ValueError for a URL that cannot be fetched safely — bad scheme, no host,
    unresolvable, or resolving to a private/loopback address — and for one too long to
    store. Everything that can only fail later (404, timeout, paywall) is saved and
    surfaces as a visible extraction error with a retry button.

    Both entry points (the API and the box in Saved) come through here, so the rules
    are stated once and neither door can be the lenient one.

    Credentials pasted into the address (``https://user:pass@host/article``) are split
    off before anything is stored or logged. They are used for the one extraction this
    save triggers and then forgotten: an Article row is global and shared with everyone
    else who saved the same URL, so it is not a place to keep one user's password. A
    later Retry from the article panel therefore fetches unauthenticated and reports
    the 401 it gets, which is the honest outcome of not storing the secret.
    """
    from app.utils.url_validator import async_validate_feed_url, split_url_credentials

    url, auth_user, auth_pass = split_url_credentials(url)

    # Measured after the credentials come off, since that is the address being stored.
    if len(url) > _URL_MAX:
        raise ValueError(f"The address is too long to save (over {_URL_MAX} characters)")

    await async_validate_feed_url(url)  # ValueError → caller turns it into a toast

    normalized = normalize_url(url)
    existing = None
    if normalized:
        # trimmed_at IS NULL is load-bearing: retention trim strips the body and leaves
        # a stub that list_articles hides unconditionally, so attaching saved_at to one
        # would report success and then show nothing in Saved, forever. Treat a trimmed
        # match as no match and build a fresh article instead — the stub stays untouched
        # and keeps serving the interest profile.
        # The same article routinely exists in several feeds (hence _dedup_cross_feed
        # in the fetcher), so this can match more than one row and the choice must be
        # deterministic. Without an ORDER BY you can end up attached to a copy from a
        # feed you don't subscribe to while your own copy sits right there, and Saved
        # then shows a feed name you don't recognise.
        from app.models.feed import UserFeed

        existing = await db.scalar(
            select(Article)
            .outerjoin(
                UserFeed,
                (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id),
            )
            .where(
                Article.url_normalized == normalized,
                Article.trimmed_at.is_(None),
            )
            .order_by(
                UserFeed.id.is_(None),                      # a copy you subscribe to wins
                Article.readable_status != "success",       # then one already extracted
                Article.id,                                 # then oldest, for stability
            )
            .limit(1)
        )

    if existing is not None:
        await _upsert_saved_state(existing.id, user.id, db)
        # An article with no full text would hand the user a two-sentence RSS excerpt
        # when they pasted the URL precisely to read the whole thing. Re-extract — but
        # for a feed article only when there is genuinely nothing to show. Article rows
        # are global, so re-extracting one the user merely deduped onto flips on the
        # "extracting" spinner for every subscriber, and a failure would replace their
        # silent feed content with an error banner.
        needs_extraction = existing.readable_status != "success" and (
            existing.feed_id is None or not _has_usable_content(existing)
        )
        if needs_extraction and existing.url:
            existing.readable_status = "pending"
            existing.readable_error = None
            # Restores the backoff attempts, and readable_active is
            # (pending AND NOT readable_retries) — a leftover count would leave the
            # user watching a re-extracting article with no progress indicator.
            existing.readable_retries = 0
            existing.readable_failed_at = None
            existing.readable_next_retry_at = _buffer_until()
            await db.commit()
            # Credentials only if this row is the address they were pasted with. A
            # match is found by normalized URL, so the stored address can be a copy
            # from another feed on another host, and that host has no business
            # receiving them.
            same_address = existing.url == url
            asyncio.create_task(_import_saved_bg(
                existing.id, user.id, existing.url,
                auth_user if same_address else None,
                auth_pass if same_address else None,
            ))
        else:
            await db.commit()
            # Nothing to extract, so nothing will call the post-extraction pass later.
            # Without this the article's filters depend on who saved it first: the user
            # whose save triggered the extraction gets them, and everyone deduping onto
            # the finished article afterwards does not. finalize_saved_article decides
            # whether they are owed, subscription included.
            await finalize_saved_article(existing, user.id, db)
            await db.commit()
        return existing, True

    # No truncation anywhere below: the guard above means the address fits, so the
    # stored URL, the guid and the hash all describe the same string. Cutting them
    # here would have let the hash and the guid disagree about which address this is.
    article = Article(
        feed_id=None,
        guid=url,
        guid_hash=hashlib.sha256(url.encode()).hexdigest(),
        url=url,
        url_normalized=normalized,
        title=title_from_url(url),
        readable_status="pending",
        readable_next_retry_at=_buffer_until(),
        fetched_at=datetime.now(timezone.utc),
    )
    db.add(article)
    await db.flush()
    await _upsert_saved_state(article.id, user.id, db)
    await db.commit()

    asyncio.create_task(_import_saved_bg(article.id, user.id, url, auth_user, auth_pass))
    return article, False


async def unsave_article(article_id: int, user_id: int, db: AsyncSession) -> None:
    """Drop the article out of this user's Saved view.

    Never deletes the Article row: it is a global record shared with every other user
    who saved the same URL. A feedless article loses access and is cleaned up later by
    the orphan purge branch; one that has a feed simply stays in that feed.
    """
    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id == article_id,
        )
    )
    if state is not None:
        state.saved_at = None
        await db.commit()


def adopt_resolved_url(article: Article, resolved_url: str | None) -> None:
    """Point a saved article at the address its content actually came from.

    What gets pasted is often a click tracker or carries campaign parameters — the
    article then shows that host as its source and "Open original" walks back through
    the tracker. Only feedless articles are touched: a feed article's URL belongs to
    the feed, and rewriting it would move the ground under the fetcher's own dedup.

    The rewrite is deliberately not merged with an article that may already hold the
    resolved address: the row is on screen and being polled, so swapping its identity
    mid-flight costs more than the duplicate it would save.

    Credentials are stripped here too. The resolved address can be a canonical link
    read off the page, which is the host's text and may carry userinfo — this is the
    one door left through which it could reach a stored column. Being the host's text
    is also why the length is checked rather than cut to fit: the saved address works,
    and trading it for the first 2048 characters of a longer one trades it for a link
    that goes nowhere.
    """
    from app.utils.url_validator import split_url_credentials

    if article.feed_id is not None or not resolved_url:
        return
    resolved_url = split_url_credentials(resolved_url)[0]
    if resolved_url == article.url or len(resolved_url) > _URL_MAX:
        return
    article.url = resolved_url
    article.url_normalized = normalize_url(resolved_url)


async def _import_saved_bg(
    article_id: int,
    user_id: int,
    url: str,
    auth_user: str | None = None,
    auth_pass: str | None = None,
) -> None:
    """Background extraction for a freshly saved URL.

    *auth_user* / *auth_pass* are the credentials the URL was pasted with, held only
    for the length of this task. Passing them explicitly rather than leaving them in
    the address is what keeps them out of the fetcher's log lines and puts them behind
    the same origin check as feed credentials, so a redirect off the host does not
    take them along.
    """
    from app.database import async_session_factory

    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None, extract_readable_with_title, url, auth_user, auth_pass, True
        )
    except Exception as exc:
        result = ReadableResult(error=str(exc)[:200])
        logger.warning("saved import: extraction error for article %d: %s", article_id, exc)

    # Everything below is wrapped because this runs as a detached task: an escaping
    # exception would vanish with it, and the article would already be in a terminal
    # state, so process_pending_readable — which only picks up 'pending' rows — would
    # never come back for it. That is the same silent miss finalize_for_all_savers
    # exists to prevent, just through another door.
    try:
        async with async_session_factory() as db:
            article = await db.scalar(select(Article).where(Article.id == article_id))
            if article is None:
                return
            if article.readable_status == "success":
                return  # the batch worker got there first
            apply_readable_result(
                article, result.content, result.error, result.http_status,
                result.published_at, title=result.title, description=result.description,
            )
            adopt_resolved_url(article, result.resolved_url)
            await db.commit()
            await finalize_saved_article(article, user_id, db)
            await db.commit()
    except Exception:
        logger.exception(
            "saved import: post-processing failed for article %d user %d",
            article_id, user_id,
        )
        return
    logger.info(
        "saved import: article=%d user=%d status=%s",
        article_id, user_id, result.error or "ok",
    )


async def finalize_saved_article(
    article: Article, user_id: int, db: AsyncSession
) -> None:
    """Post-extraction pass for one saved article and one user: filters, then summary.

    The rule it enforces: your filters run once on an article you saved, unless they
    have already run on it because it arrived through a feed you subscribe to. Three
    guards, all here rather than in the callers, since every caller needs all three:

    * **Terminal state.** ``apply_readable_result`` leaves the status at 'pending' on a
      transient error and schedules a backoff, so "the extraction call returned" is not
      the same as "extraction is done". Running filters then would re-apply
      star/archive/mark-read after every retry. 'skipped' counts as terminal: it means
      extraction will not run at all (a full-content feed, or one that blocked us), so
      nothing further is coming for that article either.
    * **filters_applied_at.** The terminal check only stops repeats within one attempt.
      An article can reach a terminal state *twice*: it ends 'failed', a second user's
      save pushes it back to 'pending' and re-extracts, and this time it succeeds. The
      first user's filter actions would then be re-applied minutes after they undid
      them. Stamping the first run closes that by construction.
    * **Subscription.** The feed fetch runs every subscriber's filters on every article
      it brings in, and it stamps nothing, so for a subscriber the pass is already done
      and invisible to the guard above. Saving such an article (a pasted URL that
      deduped onto it) would run the same filters a second time and undo whatever the
      reader had undone since. Deliberately not stamped when skipped for this reason:
      nothing ran, and the column says it did.
    """
    from app.models.feed import UserFeed
    from app.services.ai_pipeline_service import maybe_enqueue_starred_summary
    from app.services.filter_service import apply_filters_to_saved_article

    if article.readable_status not in ("success", "failed", "skipped"):
        return

    state = await db.scalar(
        select(UserArticleState).where(
            UserArticleState.user_id == user_id,
            UserArticleState.article_id == article.id,
        )
    )
    if state is None or state.saved_at is None or state.filters_applied_at is not None:
        return

    if article.feed_id is not None and await db.scalar(
        select(UserFeed.id)
        .where(UserFeed.feed_id == article.feed_id, UserFeed.user_id == user_id)
        .limit(1)
    ):
        return

    await apply_filters_to_saved_article(article, user_id, db)
    state.filters_applied_at = datetime.now(timezone.utc)
    await db.flush()

    # No scoring for saved articles, so no AI-filter stage either. The summary follows
    # the same rule as feed articles: only when the user opted into auto-summaries for
    # starred articles and a filter just starred this one.
    await maybe_enqueue_starred_summary(article, user_id, db)


async def finalize_for_all_savers(article: Article, db: AsyncSession) -> None:
    """Run the post-extraction pass for every user who saved this article.

    The batch worker's entry point. It has no idea which import task died, and filters
    are per-user, so it fans out over the savers; ``finalize_saved_article`` makes each
    one a no-op if it already ran.
    """
    user_ids = (await db.execute(
        select(UserArticleState.user_id).where(
            UserArticleState.article_id == article.id,
            UserArticleState.saved_at.is_not(None),
        )
    )).scalars().all()
    for user_id in user_ids:
        await finalize_saved_article(article, user_id, db)

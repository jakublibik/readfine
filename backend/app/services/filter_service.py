"""Filter service: CRUD, condition evaluation, and filter application during fetch."""
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from functools import lru_cache

import regex as _regex

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import Article, ArticleAiJob, UserArticleState
from app.models.feed import Folder, UserFeed
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import ArticleLabel, Label
from app.schemas.filter import FilterCreate, FilterResponse, FilterTestResult, FilterTestSample, FilterUpdate
from app.services.scope_tokens import token_matches_article

logger = logging.getLogger(__name__)

_AI_CONDITION_FIELDS = frozenset({"ai_score"})
_AI_SCORE_ALLOWED_OPERATORS = frozenset({"equals", "gt", "lt"})

# Canonical filter ordering. Every place that lists or executes filters must use
# this same ordering, so the settings list shows filters in the exact order they
# run (position ties are common — the form defaults to 0 — and `stop_on_match`
# makes execution order user-visible). `Filter.id` breaks name ties.
FILTER_ORDER = (Filter.position, func.lower(Filter.name), Filter.id)

_REGEX_MAX_LEN = 200
# Fast create-time UX reject for a few obviously dangerous shapes. This is NOT a
# security boundary (it is easily bypassed, e.g. `([a-z]+)*`); the real guard is
# the evaluation-time timeout below, which bounds any pattern regardless of shape.
_REDOS_PATTERNS = re.compile(r"(\(.*\*.*\*|\(.*\+.*\+|\(\w\+\)\+|\(\w\*\)\*|\(\w\+\)\{)")

# Evaluation-time ReDoS guard. User-supplied filter regexes run synchronously on
# the event loop during fetch (apply_filters_to_new_articles), filter test, and
# retroactive apply. CPython's stdlib `re` has no timeout and does not release the
# GIL while matching, so a catastrophic-backtracking pattern would freeze the whole
# process for every user. The third-party `regex` module supports a per-match
# ``timeout`` (raising ``TimeoutError``), so a pathological pattern is capped instead
# of hanging. The input is also truncated as a belt-and-suspenders safety net — the
# cap is far larger than any real article body, so legitimate matches are unaffected.
#
# The budget is generous on purpose. It is wall-clock time measured on a shared box,
# and an aborted match is silently reported as "no match" — a filter the user wrote
# simply does not fire. A cap tight enough to occasionally catch an innocent pattern
# buys nothing: `\bAI\b` over the full input cap costs ~25 ms, and 0.1 s left so
# little headroom that a busy fetch cycle tripped it in production. One second still
# stops a catastrophically backtracking pattern from freezing the event loop, which
# is the only thing this guard exists for.
_REGEX_MATCH_TIMEOUT_S = 1.0
_REGEX_INPUT_CAP = 1_000_000


@lru_cache(maxsize=512)
def _compile_user_regex(pattern: str):
    """Compile a user filter regex once and cache it (evaluate_filter runs per
    article). Returns the compiled pattern, or None if it does not compile."""
    try:
        return _regex.compile(pattern, _regex.IGNORECASE)
    except _regex.error:
        return None

# Retroactive apply: commit every N articles to keep transactions short; chunk
# bulk scoring inserts to keep statements bounded.
_RETRO_COMMIT_BATCH = 200
_RETRO_SCORING_CHUNK = 500


def is_ai_filter(f: "Filter") -> bool:
    return any(c.field in _AI_CONDITION_FIELDS for c in f.conditions)


def _validate_ai_conditions(conditions) -> None:
    for c in conditions:
        if c.field != "ai_score":
            continue
        if c.operator not in _AI_SCORE_ALLOWED_OPERATORS:
            raise ValueError(
                f"Operator '{c.operator}' is not allowed for ai_score — use equals, gt, or lt."
            )
        try:
            val = float(c.value)
        except (ValueError, TypeError):
            raise ValueError("ai_score value must be a number.")
        if not (0 <= val <= 100):
            raise ValueError("ai_score value must be between 0 and 100.")


def _validate_published_at_conditions(conditions) -> None:
    for c in conditions:
        if c.field != "published_at":
            continue
        try:
            date.fromisoformat(c.value.strip())
        except ValueError:
            raise ValueError("published_at value must be a valid date in YYYY-MM-DD format.")


def _validate_regex_conditions(conditions) -> None:
    """Raise ValueError if any regex condition is too long or looks like a ReDoS risk."""
    for c in conditions:
        if c.operator != "regex":
            continue
        if len(c.value) > _REGEX_MAX_LEN:
            raise ValueError(f"Regex pattern too long (max {_REGEX_MAX_LEN} characters).")
        if _REDOS_PATTERNS.search(c.value):
            raise ValueError("Regex pattern contains potentially unsafe constructs (nested quantifiers).")
        try:
            re.compile(c.value)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e


# ── CRUD helpers ─────────────────────────────────────────────────────────────

_MAX_SCOPE_ITEMS = 50


async def _validate_scope_list(user_id: int, scope_list: list[str], db: AsyncSession) -> None:
    """Raise ValueError if scope list is too long or contains items not belonging to the user."""
    if len(scope_list) > _MAX_SCOPE_ITEMS:
        raise ValueError(f"Too many scope items (max {_MAX_SCOPE_ITEMS})")

    feed_ids: list[int] = []
    folder_ids: list[int] = []
    for item in scope_list:
        try:
            if item.startswith("feed:"):
                feed_ids.append(int(item[5:]))
            elif item.startswith("folder:"):
                folder_id = int(item[7:])
                if folder_id != 0:  # 0 = sentinel for "no folder", no DB check needed
                    folder_ids.append(folder_id)
            else:
                raise ValueError(f"Invalid scope item: {item!r}")
        except (ValueError, IndexError) as exc:
            raise ValueError(str(exc)) from exc

    if feed_ids:
        result = await db.execute(
            select(UserFeed.feed_id).where(
                UserFeed.user_id == user_id, UserFeed.feed_id.in_(feed_ids)
            )
        )
        found = {row[0] for row in result}
        missing = sorted(set(feed_ids) - found)
        if missing:
            raise ValueError(f"Feed(s) not in your subscriptions: {missing}")

    if folder_ids:
        result = await db.execute(
            select(Folder.id).where(
                Folder.id.in_(folder_ids), Folder.user_id == user_id
            )
        )
        found = {row[0] for row in result}
        missing = sorted(set(folder_ids) - found)
        if missing:
            raise ValueError(f"Folder(s) not found: {missing}")


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def list_filters(user_id: int, db: AsyncSession) -> list[FilterResponse]:
    result = await db.execute(
        select(Filter)
        .where(Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(*FILTER_ORDER)
    )
    return [FilterResponse.model_validate(f) for f in result.scalars()]


async def _validate_label_actions(user_id: int, actions, db: AsyncSession) -> None:
    """Raise ValueError if any label action has a missing or invalid action_value."""
    label_ids = []
    for a in actions:
        if a.action_type != "label":
            continue
        if not a.action_value:
            raise ValueError("Label action requires a label to be selected.")
        try:
            label_ids.append(int(a.action_value))
        except (ValueError, TypeError):
            raise ValueError(f"Invalid label id: {a.action_value!r}")
    if label_ids:
        result = await db.execute(
            select(Label.id).where(Label.user_id == user_id, Label.id.in_(label_ids))
        )
        found = {row[0] for row in result}
        missing = sorted(set(label_ids) - found)
        if missing:
            raise ValueError(f"Label(s) not found: {missing}")


async def create_filter(user_id: int, payload: FilterCreate, db: AsyncSession) -> FilterResponse:
    _validate_regex_conditions(payload.conditions)
    _validate_ai_conditions(payload.conditions)
    _validate_published_at_conditions(payload.conditions)
    await _validate_scope_list(user_id, payload.scope_include, db)
    await _validate_scope_list(user_id, payload.scope_except, db)
    await _validate_label_actions(user_id, payload.actions, db)
    f = Filter(
        user_id=user_id,
        name=payload.name,
        is_active=payload.is_active,
        match_operator=payload.match_operator,
        position=payload.position,
        stop_on_match=payload.stop_on_match,
        scope_include=json.dumps(payload.scope_include) if payload.scope_include else None,
        scope_except=json.dumps(payload.scope_except) if payload.scope_except else None,
    )
    for c in payload.conditions:
        f.conditions.append(FilterCondition(**c.model_dump()))
    for a in payload.actions:
        f.actions.append(FilterAction(**a.model_dump()))
    db.add(f)
    await db.commit()
    await db.refresh(f)
    # Re-load relationships after commit
    result = await db.execute(
        select(Filter)
        .where(Filter.id == f.id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    return FilterResponse.model_validate(result.scalar_one())


async def get_filter(user_id: int, filter_id: int, db: AsyncSession) -> FilterResponse | None:
    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id, Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    f = result.scalar_one_or_none()
    return FilterResponse.model_validate(f) if f else None


async def update_filter(
    user_id: int, filter_id: int, payload: FilterUpdate, db: AsyncSession
) -> FilterResponse | None:
    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id, Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    f = result.scalar_one_or_none()
    if not f:
        return None

    if payload.conditions is not None:
        _validate_regex_conditions(payload.conditions)
        _validate_ai_conditions(payload.conditions)
        _validate_published_at_conditions(payload.conditions)

    if payload.scope_include is not None:
        await _validate_scope_list(user_id, payload.scope_include, db)
    if payload.scope_except is not None:
        await _validate_scope_list(user_id, payload.scope_except, db)
    if payload.actions is not None:
        await _validate_label_actions(user_id, payload.actions, db)

    scalar_fields = payload.model_dump(
        exclude_unset=True, exclude={"conditions", "actions", "scope_include", "scope_except"}
    )
    for field, value in scalar_fields.items():
        setattr(f, field, value)
    if "scope_include" in (payload.model_fields_set or set()):
        f.scope_include = json.dumps(payload.scope_include) if payload.scope_include else None
    if "scope_except" in (payload.model_fields_set or set()):
        f.scope_except = json.dumps(payload.scope_except) if payload.scope_except else None
    f.updated_at = datetime.now(timezone.utc)

    if payload.conditions is not None:
        for c in list(f.conditions):
            await db.delete(c)
        f.conditions.clear()
        for c in payload.conditions:
            f.conditions.append(FilterCondition(**c.model_dump()))

    if payload.actions is not None:
        for a in list(f.actions):
            await db.delete(a)
        f.actions.clear()
        for a in payload.actions:
            f.actions.append(FilterAction(**a.model_dump()))

    await db.commit()
    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    return FilterResponse.model_validate(result.scalar_one())


async def delete_filter(user_id: int, filter_id: int, db: AsyncSession) -> bool:
    result = await db.execute(
        select(Filter).where(Filter.id == filter_id, Filter.user_id == user_id)
    )
    f = result.scalar_one_or_none()
    if not f:
        return False
    await db.delete(f)
    await db.commit()
    return True


# ── Condition evaluation ──────────────────────────────────────────────────────

def _get_field_value(article: Article, user_feed: UserFeed | None, field: str, state=None):
    if field == "title":
        return article.title or ""
    if field == "content":
        return article.content or ""
    if field == "author":
        return article.author or ""
    if field == "url":
        return article.url or ""
    if field == "published_at":
        return article.published_at
    if field == "ai_score":
        if state is None or state.ai_score is None:
            return None
        return state.ai_score * 100  # stored 0.0–1.0, UI uses 0–100
    return None


def _eval_op(op: str, val: str, field_value) -> bool:
    """Evaluate a single operator/value against a field value."""
    if field_value is None:
        return op == "not_contains"
    if op == "contains":
        return val.lower() in str(field_value).lower()
    if op == "not_contains":
        return val.lower() not in str(field_value).lower()
    if op == "equals":
        if isinstance(field_value, datetime):
            try:
                return field_value.date() == datetime.fromisoformat(val).date()
            except ValueError:
                return False
        return str(field_value) == val
    if op == "regex":
        compiled = _compile_user_regex(val)
        if compiled is None:
            return False
        text = str(field_value)
        if len(text) > _REGEX_INPUT_CAP:
            text = text[:_REGEX_INPUT_CAP]
        try:
            return bool(compiled.search(text, timeout=_REGEX_MATCH_TIMEOUT_S))
        except TimeoutError:
            logger.warning(
                "filter regex timed out after %.2fs (pattern=%r) — treated as no match",
                _REGEX_MATCH_TIMEOUT_S, val[:100],
            )
            return False
    if op in ("gt", "lt"):
        if isinstance(field_value, datetime):
            try:
                cmp_date = date.fromisoformat(val.strip())
                return field_value.date() > cmp_date if op == "gt" else field_value.date() < cmp_date
            except ValueError:
                return False
        try:
            fv = float(field_value)
            cv = float(val)
            return fv > cv if op == "gt" else fv < cv
        except (ValueError, TypeError):
            return False
    return False


def _matches_condition(condition: FilterCondition, article: Article, user_feed: UserFeed | None, state=None) -> bool:
    op = condition.operator
    val = condition.value.strip()
    if not val:
        return False

    if condition.field == "title_or_content":
        title_match = _eval_op(op, val, _get_field_value(article, user_feed, "title"))
        content_match = _eval_op(op, val, _get_field_value(article, user_feed, "content"))
        # not_contains: neither field may contain the value (AND)
        # all other operators: either field suffices (OR)
        if op == "not_contains":
            return title_match and content_match
        return title_match or content_match

    return _eval_op(op, val, _get_field_value(article, user_feed, condition.field, state))


def _parse_scope_list(value: str | None) -> list[str] | None:
    """Parse a JSON scope list.

    Returns:
        []   — value is empty/null (intentional "all" for scope_include)
        list — successfully parsed
        None — JSON parse error (corrupt data; caller decides semantics)
    """
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return [item for item in parsed if isinstance(item, str)]
    except (json.JSONDecodeError, TypeError):
        return None


def _scope_matches(f: Filter, article: Article, user_feed: UserFeed | None) -> bool:
    """Return True if the article is within the filter's scope (and not excluded)."""
    # Inclusion: None = parse error → fail-closed; [] = all feeds; list = specific items
    include_list = _parse_scope_list(f.scope_include)
    if include_list is None:
        logger.warning("filter %s: corrupt scope_include — skipping (fail-closed)", getattr(f, "id", "?"))
        return False
    if include_list and not any(token_matches_article(item, article, user_feed) for item in include_list):
        return False

    # Exclusion: None = parse error → ignore (fail-safe: don't exclude anything)
    except_list = _parse_scope_list(f.scope_except) or []
    if any(token_matches_article(item, article, user_feed) for item in except_list):
        return False

    return True


def evaluate_filter(f: Filter, article: Article, user_feed: UserFeed | None = None, state=None) -> bool:
    """Return True if the article is in scope and all/any conditions match."""
    if not _scope_matches(f, article, user_feed):
        return False
    if not f.conditions:
        return False
    results = [_matches_condition(c, article, user_feed, state) for c in f.conditions]
    return all(results) if f.match_operator == "AND" else any(results)


# ── Action execution ──────────────────────────────────────────────────────────

async def _execute_actions(
    f: Filter, article: Article, user_id: int, user_feed: UserFeed, db: AsyncSession
) -> bool:
    """Execute filter actions for an article. Returns True if any action changed DB state."""
    changed = False
    for action in f.actions:
        try:
            if action.action_type == "label" and action.action_value:
                label_id = int(action.action_value)
                # Verify label belongs to this user
                label_check = await db.execute(
                    select(Label.id).where(Label.id == label_id, Label.user_id == user_id)
                )
                if not label_check.scalar_one_or_none():
                    continue
                existing = await db.execute(
                    select(ArticleLabel).where(
                        ArticleLabel.user_id == user_id,
                        ArticleLabel.article_id == article.id,
                        ArticleLabel.label_id == label_id,
                    )
                )
                if not existing.scalar_one_or_none():
                    db.add(ArticleLabel(
                        user_id=user_id,
                        article_id=article.id,
                        label_id=label_id,
                        assigned_by_filter=True,
                    ))
                    changed = True

            elif action.action_type in ("mark_read", "star", "archive"):
                state_result = await db.execute(
                    select(UserArticleState).where(
                        UserArticleState.user_id == user_id,
                        UserArticleState.article_id == article.id,
                    )
                )
                state = state_result.scalar_one_or_none()
                if state is None:
                    state = UserArticleState(user_id=user_id, article_id=article.id)
                    db.add(state)

                if action.action_type == "mark_read" and not state.is_read:
                    state.is_read = True
                    state.read_at = datetime.now(timezone.utc)
                    changed = True
                elif action.action_type == "star" and not state.is_starred:
                    # Filter star sets is_starred ONLY — deliberately not the
                    # user-intent metadata (user_starred / ever_starred / starred_at,
                    # i.e. _apply_star_side_effects). An automated star is not a
                    # user-interest signal: setting those would pollute the AI
                    # preference profile, stats (starred_count, "AI got it wrong")
                    # and retention with automation. Same principle as is_read.
                    state.is_starred = True
                    changed = True
                elif action.action_type == "archive" and not state.is_archived:
                    # Archive removes the article from the inbox/feed views and
                    # exempts it from retention purge (see purge_service). Unlike
                    # mark_read it does not touch is_read — an archived article
                    # can still be unread.
                    state.is_archived = True
                    changed = True

        except Exception as exc:
            logger.warning(
                "Filter %d action '%s' failed for article %d: %s",
                f.id, action.action_type, article.id, exc,
            )
    return changed


async def apply_filters_to_new_articles(
    feed_id: int, articles: "list[Article]", db: AsyncSession
) -> None:
    """Apply all subscribers' active filters to a batch of newly saved articles.

    Subscribers and their filters are loaded once for the whole fetch (all
    articles belong to the same feed), not per article.
    """
    if not articles:
        return

    subscribers_result = await db.execute(
        select(UserFeed).where(UserFeed.feed_id == feed_id)
    )
    user_feeds = subscribers_result.scalars().all()
    if not user_feeds:
        return

    # Batch-load every subscriber's active filters in one query, then group by
    # user_id — avoids a per-subscriber/per-article SELECT on this fetch hot path.
    filters_result = await db.execute(
        select(Filter)
        .where(
            Filter.user_id.in_([uf.user_id for uf in user_feeds]),
            Filter.is_active == True,
        )
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(*FILTER_ORDER)
    )
    filters_by_user: dict[int, list[Filter]] = {}
    for f in filters_result.scalars():
        filters_by_user.setdefault(f.user_id, []).append(f)

    for uf in user_feeds:
        filters = filters_by_user.get(uf.user_id, [])
        if not filters:
            continue
        for article in articles:
            await _apply_user_filters_to_article(article, uf, filters, db)


async def _apply_user_filters_to_article(
    article: Article, uf: UserFeed, filters: "list[Filter]", db: AsyncSession
) -> None:
    """Run one subscriber's (non-AI) filters against a single article."""
    got_star_or_label = False
    got_label = False
    for f in filters:
        if is_ai_filter(f):
            continue
        if evaluate_filter(f, article, uf):
            action_types = {a.action_type for a in f.actions}
            if action_types & {"star", "label"}:
                got_star_or_label = True
            if "label" in action_types:
                got_label = True
            await _execute_actions(f, article, uf.user_id, uf, db)
            if f.stop_on_match:
                break

    if got_star_or_label and uf.extract_readable and article.readable_status == "skipped":
        article.readable_status = "pending"

    # Enqueue scoring for labeled articles on non-readable feeds (or feeds with readable already done)
    if got_label and (not uf.extract_readable or article.readable_status == "success"):
        from app.services.ai_scoring_service import enqueue_scoring_job
        await enqueue_scoring_job(article, uf.user_id, db)


# ── AI filter batch processing ────────────────────────────────────────────────

_AI_FILTER_BATCH_SIZE = 50


async def process_ai_filters_batch(db: AsyncSession) -> int:
    """Apply AI filters to articles that have a fresh ai_score (ai_filters_applied=false)."""
    from app.models.article import UserArticleState
    from app.services.ai_jobs import ai_enabled_globally

    if not await ai_enabled_globally(db):
        return 0

    states_result = await db.execute(
        select(UserArticleState)
        .where(
            UserArticleState.ai_score.isnot(None),
            UserArticleState.ai_filters_applied == False,  # noqa: E712
        )
        .limit(_AI_FILTER_BATCH_SIZE)
    )
    states = states_result.scalars().all()
    if not states:
        return 0

    article_ids = list({s.article_id for s in states})
    user_ids = list({s.user_id for s in states})

    articles_map: dict[int, Article] = {
        a.id: a
        for a in (await db.scalars(select(Article).where(Article.id.in_(article_ids)))).all()
    }

    # Batch-load all users' active AI filters in one query, then group by user_id.
    filters_result = await db.execute(
        select(Filter)
        .where(Filter.user_id.in_(user_ids), Filter.is_active == True)  # noqa: E712
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(*FILTER_ORDER)
    )
    filters_by_user: dict[int, list[Filter]] = {}
    for f in filters_result.scalars():
        if is_ai_filter(f):
            filters_by_user.setdefault(f.user_id, []).append(f)

    user_feeds_result = await db.execute(
        select(UserFeed).where(UserFeed.user_id.in_(user_ids))
    )
    feed_user_map: dict[tuple[int, int], UserFeed] = {
        (uf.user_id, uf.feed_id): uf for uf in user_feeds_result.scalars()
    }

    for state in states:
        article = articles_map.get(state.article_id)
        if not article:
            state.ai_filters_applied = True
            continue

        uf = feed_user_map.get((state.user_id, article.feed_id))
        await _apply_ai_filters_for_state(state, article, uf, filters_by_user.get(state.user_id, []), db)

    await db.commit()
    logger.info("ai_filters: processed %d states", len(states))
    return len(states)


async def _apply_ai_filters_for_state(
    state: "UserArticleState",
    article: Article,
    uf: "UserFeed | None",
    filters: "list[Filter]",
    db: AsyncSession,
) -> None:
    """Evaluate AI filters and execute actions for a single article state. Does not commit."""
    for f in filters:
        if evaluate_filter(f, article, uf, state):
            await _execute_actions(f, article, state.user_id, uf, db)
            if f.stop_on_match:
                break
    state.ai_filters_applied = True


# ── Test / retroactive apply ──────────────────────────────────────────────────

async def test_filter(user_id: int, filter_id: int, db: AsyncSession) -> FilterTestResult | None:
    """Preview how many existing articles the filter would match."""
    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id, Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    f = result.scalar_one_or_none()
    if not f:
        return None

    from app.models.feed import Feed

    user_feeds_result = await db.execute(
        select(UserFeed).where(UserFeed.user_id == user_id)
    )
    user_feeds_map = {uf.feed_id: uf for uf in user_feeds_result.scalars()}

    # Narrow the scan to the filter's scope when possible — keeps the full,
    # unlimited match count accurate without loading the whole archive.
    scope_feed_ids = _scope_feed_ids(f, user_feeds_map)
    articles_q = (
        select(Article, Feed.title.label("feed_title"))
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .join(Feed, Feed.id == Article.feed_id)
        .where(UserFeed.user_id == user_id)
        .order_by(Article.published_at.desc())
    )
    if scope_feed_ids is not None:
        if not scope_feed_ids:
            return FilterTestResult(matched_count=0, samples=[])
        articles_q = articles_q.where(Article.feed_id.in_(scope_feed_ids))
    rows = (await db.execute(articles_q)).all()

    states_map: dict[int, "UserArticleState"] = {}
    if is_ai_filter(f):
        from app.models.article import UserArticleState
        article_ids = [a.id for a, _ in rows]
        if article_ids:
            states_result = await db.execute(
                select(UserArticleState).where(
                    UserArticleState.user_id == user_id,
                    UserArticleState.article_id.in_(article_ids),
                )
            )
            states_map = {s.article_id: s for s in states_result.scalars()}

    matched = [
        (a, ft)
        for a, ft in rows
        if evaluate_filter(f, a, user_feeds_map.get(a.feed_id), states_map.get(a.id))
    ]
    return FilterTestResult(
        matched_count=len(matched),
        samples=[
            FilterTestSample(title=a.title or "(no title)", feed_title=ft or "")
            for a, ft in matched[:5]
        ],
    )


@dataclass
class _RetroItem:
    """One matched article and the scoring decisions for it."""
    article: Article
    uf: "UserFeed | None"
    readable_pending: bool   # apply: flip readable_status → "pending" (eventual scoring)
    direct_enqueue: bool     # apply: insert a scoring job immediately
    will_score: bool         # preview: this article will be scored (direct or via readable)


@dataclass
class RetroApplyPlan:
    """Result of a single scan: what a retroactive apply would do."""
    f: Filter
    is_ai: bool              # filter has an ai_score condition
    has_label_action: bool
    items: "list[_RetroItem]"
    scoring_count: int       # matched articles that will be queued for AI scoring


def _scope_feed_ids(f: Filter, user_feeds_map: "dict[int, UserFeed]") -> "set[int] | None":
    """Resolve the feed_ids a filter's scope_include limits to, for SQL narrowing.

    Returns None when the scope spans all feeds (no narrowing possible),
    an empty set when scope is corrupt (fail-closed, matches _scope_matches).
    """
    include = _parse_scope_list(f.scope_include)
    if include is None:
        return set()        # corrupt → fail-closed
    if not include:
        return None         # [] → all feeds
    feed_ids: set[int] = set()
    for item in include:
        if item.startswith("feed:"):
            try:
                feed_ids.add(int(item[5:]))
            except ValueError:
                pass
        elif item.startswith("folder:"):
            try:
                folder_val = int(item[7:])
            except ValueError:
                continue
            for uf in user_feeds_map.values():
                if folder_val == 0 and uf.folder_id is None:
                    feed_ids.add(uf.feed_id)
                elif uf.folder_id == folder_val:
                    feed_ids.add(uf.feed_id)
        else:
            return None     # unknown token → don't narrow (be safe)
    return feed_ids


async def _plan_retroactive_apply(
    user_id: int, filter_id: int, db: AsyncSession
) -> "RetroApplyPlan | None":
    """Single scan that both preview and apply consume — guarantees the previewed
    scoring count matches what apply actually enqueues."""
    from app.models.user import UserSettings
    from app.services.ai_jobs import ai_enabled_globally
    from app.services.ai_scoring_service import scoring_eligible

    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id, Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    f = result.scalar_one_or_none()
    if not f:
        return None

    action_types = {a.action_type for a in f.actions}
    has_label = "label" in action_types
    has_star_or_label = bool(action_types & {"star", "label"})
    is_ai = is_ai_filter(f)

    user_feeds_result = await db.execute(
        select(UserFeed).where(UserFeed.user_id == user_id)
    )
    user_feeds_map = {uf.feed_id: uf for uf in user_feeds_result.scalars()}

    # Narrow the article scan to the filter's scope when possible (avoids loading
    # the user's entire article table just to count).
    scope_feed_ids = _scope_feed_ids(f, user_feeds_map)
    if scope_feed_ids is not None and not scope_feed_ids:
        return RetroApplyPlan(f=f, is_ai=is_ai, has_label_action=has_label,
                              items=[], scoring_count=0)

    articles_q = (
        select(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(UserFeed.user_id == user_id)
    )
    if scope_feed_ids is not None:
        articles_q = articles_q.where(Article.feed_id.in_(scope_feed_ids))
    articles = (await db.execute(articles_q)).scalars().all()

    states_map: dict[int, UserArticleState] = {}
    if is_ai and articles:
        states_result = await db.execute(
            select(UserArticleState).where(
                UserArticleState.user_id == user_id,
                UserArticleState.article_id.in_([a.id for a in articles]),
            )
        )
        states_map = {s.article_id: s for s in states_result.scalars()}

    matched = [
        a for a in articles
        if evaluate_filter(f, a, user_feeds_map.get(a.feed_id), states_map.get(a.id))
    ]

    # Scoring prerequisites loaded once. Settings-level eligibility (uf=None) gates
    # the whole batch; a per-feed override can only turn scoring *off* per article.
    ai_on = await ai_enabled_globally(db)
    settings = await db.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    scoring_possible = has_label and ai_on and scoring_eligible(settings, None)

    existing_jobs: set[int] = set()
    if scoring_possible and matched:
        existing_jobs = set(await db.scalars(
            select(ArticleAiJob.article_id).where(
                ArticleAiJob.user_id == user_id,
                ArticleAiJob.operation == "scoring",
                ArticleAiJob.article_id.in_([a.id for a in matched]),
            )
        ))

    items: list[_RetroItem] = []
    scoring_count = 0
    for a in matched:
        uf = user_feeds_map.get(a.feed_id)
        extract = bool(uf and uf.extract_readable)
        status = a.readable_status
        eligible = (
            scoring_possible
            and a.id not in existing_jobs
            and scoring_eligible(settings, uf)  # re-check per-feed override
        )
        direct_path = (not extract) or status == "success"
        via_readable = extract and status == "skipped"
        readable_pending = has_star_or_label and extract and status == "skipped"
        direct_enqueue = eligible and direct_path
        will_score = eligible and (direct_path or via_readable)
        if will_score:
            scoring_count += 1
        items.append(_RetroItem(
            article=a, uf=uf,
            readable_pending=readable_pending,
            direct_enqueue=direct_enqueue,
            will_score=will_score,
        ))

    return RetroApplyPlan(f=f, is_ai=is_ai, has_label_action=has_label,
                          items=items, scoring_count=scoring_count)


async def preview_filter_retroactive(
    user_id: int, filter_id: int, db: AsyncSession
) -> "dict | None":
    """Count what a retroactive apply would do, without writing anything."""
    plan = await _plan_retroactive_apply(user_id, filter_id, db)
    if plan is None:
        return None
    return {
        "matched": len(plan.items),
        "scoring_count": plan.scoring_count,
        "is_ai_filter": plan.is_ai,
        "has_label_action": plan.has_label_action,
    }


async def apply_filter_retroactively(
    user_id: int, filter_id: int, db: AsyncSession, enqueue_scoring: bool = True
) -> tuple[int, int, int]:
    """Apply an existing filter to the user's articles.

    With enqueue_scoring=False ("skip" mode), filter actions still run (label,
    mark_read, …) but no AI scoring is triggered — neither direct enqueue nor the
    readable→scoring path.

    Returns (matched_count, changed_count, scoring_queued):
      matched_count — articles where filter conditions evaluated to True
      changed_count — articles where at least one action actually modified DB state
      scoring_queued — AI scoring jobs enqueued (0 when enqueue_scoring=False)
    """
    plan = await _plan_retroactive_apply(user_id, filter_id, db)
    if plan is None:
        return 0, 0, 0

    f = plan.f
    changed = 0
    scoring_queued = 0
    scoring_pairs: list[tuple[int, int]] = []
    for i, item in enumerate(plan.items):
        if await _execute_actions(f, item.article, user_id, item.uf, db):
            changed += 1
        if enqueue_scoring:
            if item.readable_pending:
                item.article.readable_status = "pending"
            if item.direct_enqueue:
                scoring_pairs.append((item.article.id, user_id))
        if (i + 1) % _RETRO_COMMIT_BATCH == 0:
            await db.commit()

    if enqueue_scoring and scoring_pairs:
        from app.services.ai_scoring_service import bulk_create_scoring_jobs
        for start in range(0, len(scoring_pairs), _RETRO_SCORING_CHUNK):
            scoring_queued += await bulk_create_scoring_jobs(
                scoring_pairs[start:start + _RETRO_SCORING_CHUNK], db
            )

    await db.commit()
    return len(plan.items), changed, scoring_queued

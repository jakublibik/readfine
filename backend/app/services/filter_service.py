"""Filter service: CRUD, condition evaluation, and filter application during fetch."""
import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import Article, UserArticleState
from app.models.feed import Folder, UserFeed
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import ArticleLabel, Label
from app.schemas.filter import FilterCreate, FilterResponse, FilterTestResult, FilterTestSample, FilterUpdate

logger = logging.getLogger(__name__)


_REGEX_MAX_LEN = 200
# Patterns that commonly cause catastrophic backtracking
_REDOS_PATTERNS = re.compile(r"(\(.*\*.*\*|\(.*\+.*\+|\(\w\+\)\+|\(\w\*\)\*|\(\w\+\)\{)")


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
        .order_by(Filter.position, Filter.name)
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

def _get_field_value(article: Article, user_feed: UserFeed | None, field: str):
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
        return str(field_value) == val
    if op == "regex":
        try:
            return bool(re.search(val, str(field_value), re.IGNORECASE))
        except re.error:
            return False
    if op in ("gt", "lt"):
        if isinstance(field_value, datetime):
            try:
                cmp_dt = datetime.fromisoformat(val)
                if cmp_dt.tzinfo is None:
                    cmp_dt = cmp_dt.replace(tzinfo=timezone.utc)
                return field_value > cmp_dt if op == "gt" else field_value < cmp_dt
            except ValueError:
                return False
        try:
            fv = float(field_value)
            cv = float(val)
            return fv > cv if op == "gt" else fv < cv
        except (ValueError, TypeError):
            return False
    return False


def _matches_condition(condition: FilterCondition, article: Article, user_feed: UserFeed | None) -> bool:
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

    return _eval_op(op, val, _get_field_value(article, user_feed, condition.field))


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


def _item_matches_article(item: str, article: Article, user_feed: UserFeed | None) -> bool:
    """Return True if a single scope item (feed:ID or folder:ID) matches the article."""
    try:
        if item.startswith("feed:"):
            return article.feed_id == int(item[5:])
        if item.startswith("folder:"):
            folder_val = int(item[7:])
            if folder_val == 0:  # sentinel: feeds with no folder
                return user_feed is not None and user_feed.folder_id is None
            return user_feed is not None and user_feed.folder_id == folder_val
    except (ValueError, IndexError):
        pass
    return False


def _scope_matches(f: Filter, article: Article, user_feed: UserFeed | None) -> bool:
    """Return True if the article is within the filter's scope (and not excluded)."""
    # Inclusion: None = parse error → fail-closed; [] = all feeds; list = specific items
    include_list = _parse_scope_list(f.scope_include)
    if include_list is None:
        logger.warning("filter %s: corrupt scope_include — skipping (fail-closed)", getattr(f, "id", "?"))
        return False
    if include_list and not any(_item_matches_article(item, article, user_feed) for item in include_list):
        return False

    # Exclusion: None = parse error → ignore (fail-safe: don't exclude anything)
    except_list = _parse_scope_list(f.scope_except) or []
    if any(_item_matches_article(item, article, user_feed) for item in except_list):
        return False

    return True


def evaluate_filter(f: Filter, article: Article, user_feed: UserFeed | None = None) -> bool:
    """Return True if the article is in scope and all/any conditions match."""
    if not _scope_matches(f, article, user_feed):
        return False
    if not f.conditions:
        return False
    results = [_matches_condition(c, article, user_feed) for c in f.conditions]
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

            elif action.action_type in ("mark_read", "star", "hide"):
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
                    await db.execute(
                        update(UserFeed)
                        .where(UserFeed.user_id == user_id, UserFeed.feed_id == article.feed_id)
                        .values(unread_count=func.greatest(UserFeed.unread_count - 1, 0))
                    )
                    changed = True
                elif action.action_type == "star" and not state.is_starred:
                    state.is_starred = True
                    changed = True
                elif action.action_type == "hide" and not state.is_hidden:
                    state.is_hidden = True
                    changed = True
            # "notify" is a no-op stub for MVP

        except Exception as exc:
            logger.warning(
                "Filter %d action '%s' failed for article %d: %s",
                f.id, action.action_type, article.id, exc,
            )
    return changed


async def apply_filters_to_article(article: Article, db: AsyncSession) -> None:
    """Apply all subscribers' active filters to a newly saved article."""
    if article.feed_id is None:
        return

    subscribers_result = await db.execute(
        select(UserFeed).where(UserFeed.feed_id == article.feed_id)
    )
    user_feeds = subscribers_result.scalars().all()

    for uf in user_feeds:
        filters_result = await db.execute(
            select(Filter)
            .where(Filter.user_id == uf.user_id, Filter.is_active == True)
            .options(selectinload(Filter.conditions), selectinload(Filter.actions))
            .order_by(Filter.position)
        )
        filters = filters_result.scalars().all()

        got_star_or_label = False
        for f in filters:
            if evaluate_filter(f, article, uf):
                action_types = {a.action_type for a in f.actions}
                if action_types & {"star", "label"}:
                    got_star_or_label = True
                await _execute_actions(f, article, uf.user_id, uf, db)
                if f.stop_on_match:
                    break

        if got_star_or_label and uf.extract_readable and article.readable_status == "skipped":
            article.readable_status = "pending"


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

    articles_result = await db.execute(
        select(Article, Feed.title.label("feed_title"))
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .join(Feed, Feed.id == Article.feed_id)
        .where(UserFeed.user_id == user_id)
        .order_by(Article.published_at.desc())
        .limit(500)
    )
    rows = articles_result.all()

    matched = [(a, ft) for a, ft in rows if evaluate_filter(f, a, user_feeds_map.get(a.feed_id))]
    return FilterTestResult(
        matched_count=len(matched),
        samples=[
            FilterTestSample(title=a.title or "(no title)", feed_title=ft or "")
            for a, ft in matched[:5]
        ],
    )


async def apply_filter_retroactively(
    user_id: int, filter_id: int, db: AsyncSession
) -> tuple[int, int]:
    """Apply an existing filter to all user's articles.

    Returns (matched_count, changed_count):
      matched_count — articles where filter conditions evaluated to True
      changed_count — articles where at least one action actually modified DB state
    """
    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id, Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    f = result.scalar_one_or_none()
    if not f:
        return 0, 0

    user_feeds_result = await db.execute(
        select(UserFeed).where(UserFeed.user_id == user_id)
    )
    user_feeds_map = {uf.feed_id: uf for uf in user_feeds_result.scalars()}

    articles_result = await db.execute(
        select(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(UserFeed.user_id == user_id)
    )
    articles = articles_result.scalars().all()

    action_types = {a.action_type for a in f.actions}
    triggers_readable = bool(action_types & {"star", "label"})

    matched = 0
    changed = 0
    for article in articles:
        uf = user_feeds_map.get(article.feed_id)
        if evaluate_filter(f, article, uf):
            matched += 1
            action_changed = await _execute_actions(f, article, user_id, uf, db)
            if action_changed:
                changed += 1
                if triggers_readable and uf and uf.extract_readable and article.readable_status == "skipped":
                    article.readable_status = "pending"

    await db.commit()
    return matched, changed

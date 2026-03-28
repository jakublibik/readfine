"""Filter service: CRUD, condition evaluation, and filter application during fetch."""
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
from app.schemas.filter import FilterCreate, FilterResponse, FilterTestResult, FilterUpdate

logger = logging.getLogger(__name__)


# ── CRUD helpers ─────────────────────────────────────────────────────────────

async def _validate_scope(
    user_id: int,
    scope_type: str,
    scope_feed_id: int | None,
    scope_folder_id: int | None,
    db: AsyncSession,
) -> None:
    """Raise ValueError if scope IDs don't belong to the user."""
    if scope_type == "feed":
        if not scope_feed_id:
            raise ValueError("scope_feed_id is required for feed scope")
        result = await db.execute(
            select(UserFeed.id).where(
                UserFeed.user_id == user_id, UserFeed.feed_id == scope_feed_id
            )
        )
        if not result.scalar_one_or_none():
            raise ValueError("Feed not in your subscriptions")
    elif scope_type == "folder":
        if not scope_folder_id:
            raise ValueError("scope_folder_id is required for folder scope")
        result = await db.execute(
            select(Folder.id).where(
                Folder.id == scope_folder_id, Folder.user_id == user_id
            )
        )
        if not result.scalar_one_or_none():
            raise ValueError("Folder not found")


def _normalize_scope(payload) -> tuple[int | None, int | None]:
    """Return (scope_feed_id, scope_folder_id) with unused IDs zeroed out."""
    scope_type = getattr(payload, "scope_type", "all") or "all"
    feed_id = payload.scope_feed_id if scope_type == "feed" else None
    folder_id = payload.scope_folder_id if scope_type == "folder" else None
    return feed_id, folder_id


# ── CRUD ──────────────────────────────────────────────────────────────────────

async def list_filters(user_id: int, db: AsyncSession) -> list[FilterResponse]:
    result = await db.execute(
        select(Filter)
        .where(Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(Filter.position, Filter.name)
    )
    return [FilterResponse.model_validate(f) for f in result.scalars()]


async def create_filter(user_id: int, payload: FilterCreate, db: AsyncSession) -> FilterResponse:
    await _validate_scope(user_id, payload.scope_type, payload.scope_feed_id, payload.scope_folder_id, db)
    scope_feed_id, scope_folder_id = _normalize_scope(payload)
    f = Filter(
        user_id=user_id,
        name=payload.name,
        is_active=payload.is_active,
        match_operator=payload.match_operator,
        position=payload.position,
        stop_on_match=payload.stop_on_match,
        scope_type=payload.scope_type,
        scope_feed_id=scope_feed_id,
        scope_folder_id=scope_folder_id,
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

    scope_type = payload.scope_type if payload.scope_type is not None else f.scope_type
    scope_feed_id = payload.scope_feed_id if "scope_feed_id" in (payload.model_fields_set or set()) else f.scope_feed_id
    scope_folder_id = payload.scope_folder_id if "scope_folder_id" in (payload.model_fields_set or set()) else f.scope_folder_id
    await _validate_scope(user_id, scope_type, scope_feed_id, scope_folder_id, db)

    scalar_fields = payload.model_dump(exclude_unset=True, exclude={"conditions", "actions"})
    for field, value in scalar_fields.items():
        setattr(f, field, value)
    # Normalize: clear unused scope IDs
    if payload.scope_type is not None:
        f.scope_feed_id, f.scope_folder_id = _normalize_scope(payload)
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


def _matches_condition(condition: FilterCondition, article: Article, user_feed: UserFeed | None) -> bool:
    field_value = _get_field_value(article, user_feed, condition.field)
    op = condition.operator
    val = condition.value.strip()
    if not val:
        return False

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


def _scope_matches(f: Filter, article: Article, user_feed: UserFeed | None) -> bool:
    """Return True if the article is within the filter's scope."""
    if f.scope_type == "all":
        return True
    if f.scope_type == "feed":
        return article.feed_id == f.scope_feed_id
    if f.scope_type == "folder":
        return user_feed is not None and user_feed.folder_id == f.scope_folder_id
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
) -> None:
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
                elif action.action_type == "star":
                    state.is_starred = True
                elif action.action_type == "hide":
                    state.is_hidden = True
            # "notify" is a no-op stub for MVP

        except Exception as exc:
            logger.warning(
                "Filter %d action '%s' failed for article %d: %s",
                f.id, action.action_type, article.id, exc,
            )


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
            .where(Filter.user_id == uf.user_id, Filter.is_active == True)  # noqa: E712
            .options(selectinload(Filter.conditions), selectinload(Filter.actions))
            .order_by(Filter.position)
        )
        filters = filters_result.scalars().all()

        for f in filters:
            if evaluate_filter(f, article, uf):
                await _execute_actions(f, article, uf.user_id, uf, db)
                if f.stop_on_match:
                    break


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

    user_feeds_result = await db.execute(
        select(UserFeed).where(UserFeed.user_id == user_id)
    )
    user_feeds_map = {uf.feed_id: uf for uf in user_feeds_result.scalars()}

    articles_result = await db.execute(
        select(Article)
        .join(UserFeed, UserFeed.feed_id == Article.feed_id)
        .where(UserFeed.user_id == user_id)
        .order_by(Article.published_at.desc())
        .limit(500)
    )
    articles = articles_result.scalars().all()

    matched = [a for a in articles if evaluate_filter(f, a, user_feeds_map.get(a.feed_id))]
    return FilterTestResult(
        matched_count=len(matched),
        sample_titles=[a.title for a in matched[:5]],
    )


async def apply_filter_retroactively(user_id: int, filter_id: int, db: AsyncSession) -> int:
    """Apply an existing filter to all user's articles. Returns count of affected articles."""
    result = await db.execute(
        select(Filter)
        .where(Filter.id == filter_id, Filter.user_id == user_id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
    )
    f = result.scalar_one_or_none()
    if not f:
        return 0

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

    count = 0
    for article in articles:
        uf = user_feeds_map.get(article.feed_id)
        if evaluate_filter(f, article, uf):
            await _execute_actions(f, article, user_id, uf, db)
            count += 1

    await db.commit()
    return count

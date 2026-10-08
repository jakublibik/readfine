"""Label service: CRUD + article label assignment."""
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.article import Article
from app.models.feed import UserFeed
from app.models.filter import Filter, FilterAction
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.label import LabelCreate, LabelResponse, LabelUpdate
from app.services.article import add_article_access_joins, article_access_predicate
from app.services.saved_search_service import strip_saved_search_references


class LabelAlreadyExistsError(Exception):
    """Raised when a label would take a name another of the user's labels has."""


async def _name_taken(db: AsyncSession, user_id: int, name: str, exclude_id: int | None = None) -> bool:
    query = select(Label.id).where(Label.user_id == user_id, Label.name == name)
    if exclude_id is not None:
        query = query.where(Label.id != exclude_id)
    return await db.scalar(query) is not None


async def list_labels(user: User, db: AsyncSession) -> list[LabelResponse]:
    result = await db.execute(
        select(Label)
        .where(Label.user_id == user.id)
        .order_by(Label.position, func.lower(Label.name))
    )
    return [LabelResponse.model_validate(label) for label in result.scalars()]


async def create_label(user: User, payload: LabelCreate, db: AsyncSession) -> LabelResponse:
    if await _name_taken(db, user.id, payload.name):
        raise LabelAlreadyExistsError(payload.name)
    label = Label(
        user_id=user.id,
        name=payload.name,
        color=payload.color,
        position=payload.position,
    )
    db.add(label)
    await db.commit()
    await db.refresh(label)
    return LabelResponse.model_validate(label)


async def update_label(
    user: User, label_id: int, payload: LabelUpdate, db: AsyncSession
) -> LabelResponse | None:
    """Update a label; None if it is not the user's.

    Raises LabelAlreadyExistsError when renaming onto another label's name.
    """
    result = await db.execute(
        select(Label).where(Label.id == label_id, Label.user_id == user.id)
    )
    label = result.scalar_one_or_none()
    if not label:
        return None
    if payload.name is not None and await _name_taken(db, user.id, payload.name, exclude_id=label.id):
        raise LabelAlreadyExistsError(payload.name)
    # None means "leave as is": every column here is NOT NULL.
    for field, value in payload.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(label, field, value)
    await db.commit()
    await db.refresh(label)
    return LabelResponse.model_validate(label)


async def _drop_label_actions(db: AsyncSession, user_id: int, label_id: int) -> list[str]:
    """Remove the "add label" actions that point at a label being deleted.

    Left in place, the action would do nothing (and with it, the scoring a label
    triggers), the filter list would show a bare id, and saving the filter would
    fail on "Label action requires a label". A filter with no action left is
    switched off. Returns the names of filters switched off here, for the user.
    Does not commit.
    """
    filters = (await db.execute(
        select(Filter)
        .join(FilterAction, FilterAction.filter_id == Filter.id)
        .where(
            Filter.user_id == user_id,
            FilterAction.action_type == "label",
            FilterAction.action_value == str(label_id),
        )
        .options(selectinload(Filter.actions))
        .distinct()
    )).scalars().all()
    switched_off = []
    for f in filters:
        f.actions = [
            a for a in f.actions
            if not (a.action_type == "label" and a.action_value == str(label_id))
        ]
        if not f.actions and f.is_active:
            f.is_active = False
            switched_off.append(f.name)
    return switched_off


async def delete_label(user: User, label_id: int, db: AsyncSession) -> list[str] | None:
    """Delete one of the user's labels. Returns the names of filters switched off
    because the label was their only action, or None when the label is not theirs."""
    result = await db.execute(
        select(Label).where(Label.id == label_id, Label.user_id == user.id)
    )
    label = result.scalar_one_or_none()
    if not label:
        return None
    await strip_saved_search_references(db, kind="label", ref_id=label.id, user_id=user.id)
    switched_off = await _drop_label_actions(db, user.id, label.id)
    await db.delete(label)
    await db.commit()
    return switched_off


async def assign_label(
    user: User, article_id: int, label_id: int, db: AsyncSession
) -> bool:
    """Assign a label to an article. Returns False if label or article not accessible to user."""
    label_exists = await db.execute(
        select(Label.id).where(Label.id == label_id, Label.user_id == user.id)
    )
    if not label_exists.scalar_one_or_none():
        return False

    article_access = await db.execute(
        add_article_access_joins(select(Article.id), user.id).where(
            Article.id == article_id,
            article_access_predicate(),
        )
    )
    if not article_access.scalar_one_or_none():
        return False

    existing = await db.execute(
        select(ArticleLabel).where(
            ArticleLabel.user_id == user.id,
            ArticleLabel.article_id == article_id,
            ArticleLabel.label_id == label_id,
        )
    )
    if existing.scalar_one_or_none():
        return True  # already assigned

    db.add(ArticleLabel(user_id=user.id, article_id=article_id, label_id=label_id))
    await db.commit()

    await _enqueue_scoring_for_label(user.id, article_id, db)
    return True


async def _enqueue_scoring_for_label(user_id: int, article_id: int, db: AsyncSession) -> None:
    """Trigger AI scoring for a freshly labeled article, mirroring the filter
    label→scoring path (see filter_service.apply_filters_to_new_articles).

    Either enqueue a scoring job now (no readable needed, or readable already
    done) or flip readable to "pending" so the readable→scoring pipeline picks it
    up. enqueue_scoring_job is idempotent and checks eligibility itself.
    """
    article = await db.get(Article, article_id)
    if article is None:
        return

    uf = None
    if article.feed_id is not None:
        uf = await db.scalar(
            select(UserFeed).where(
                UserFeed.user_id == user_id,
                UserFeed.feed_id == article.feed_id,
            )
        )

    if uf is not None and uf.extract_readable and article.readable_status == "skipped":
        article.readable_status = "pending"
        await db.commit()
        return

    if uf is None or not uf.extract_readable or article.readable_status == "success":
        from app.services.ai_scoring_service import enqueue_scoring_job
        if await enqueue_scoring_job(article, user_id, db):
            await db.commit()


async def remove_label(
    user: User, article_id: int, label_id: int, db: AsyncSession
) -> bool:
    result = await db.execute(
        delete(ArticleLabel).where(
            ArticleLabel.user_id == user.id,
            ArticleLabel.article_id == article_id,
            ArticleLabel.label_id == label_id,
        )
    )
    await db.commit()
    return result.rowcount > 0

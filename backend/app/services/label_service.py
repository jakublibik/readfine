"""Label service: CRUD + article label assignment."""
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import Article, UserArticleState
from app.models.feed import UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.label import LabelCreate, LabelResponse, LabelUpdate


async def list_labels(user: User, db: AsyncSession) -> list[LabelResponse]:
    result = await db.execute(
        select(Label)
        .where(Label.user_id == user.id)
        .order_by(Label.position, Label.name)
    )
    return [LabelResponse.model_validate(label) for label in result.scalars()]


async def create_label(user: User, payload: LabelCreate, db: AsyncSession) -> LabelResponse:
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
    result = await db.execute(
        select(Label).where(Label.id == label_id, Label.user_id == user.id)
    )
    label = result.scalar_one_or_none()
    if not label:
        return None
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(label, field, value)
    await db.commit()
    await db.refresh(label)
    return LabelResponse.model_validate(label)


async def delete_label(user: User, label_id: int, db: AsyncSession) -> "Label | None":
    result = await db.execute(
        select(Label).where(Label.id == label_id, Label.user_id == user.id)
    )
    label = result.scalar_one_or_none()
    if not label:
        return None
    await db.delete(label)
    await db.commit()
    return label


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
        select(Article.id)
        .outerjoin(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id),
        )
        .where(
            Article.id == article_id,
            UserFeed.id.is_not(None)
            | UserArticleState.is_starred.is_(True)
            | UserArticleState.is_archived.is_(True),
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
    label→scoring path (see filter_service.apply_filters_to_article).

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

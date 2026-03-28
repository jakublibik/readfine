"""Label service: CRUD + article label assignment."""
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def delete_label(user: User, label_id: int, db: AsyncSession) -> bool:
    result = await db.execute(
        select(Label).where(Label.id == label_id, Label.user_id == user.id)
    )
    label = result.scalar_one_or_none()
    if not label:
        return False
    await db.delete(label)
    await db.commit()
    return True


async def assign_label(
    user: User, article_id: int, label_id: int, db: AsyncSession
) -> bool:
    """Assign a label to an article. Returns False if label doesn't belong to user."""
    label_exists = await db.execute(
        select(Label.id).where(Label.id == label_id, Label.user_id == user.id)
    )
    if not label_exists.scalar_one_or_none():
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
    return True


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

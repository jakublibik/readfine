from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.user import User
from app.schemas.label import ArticleLabelAssign, LabelCreate, LabelResponse, LabelUpdate
from app.services.label_service import (
    LabelAlreadyExistsError,
    assign_label,
    create_label,
    delete_label,
    list_labels,
    remove_label,
    update_label,
)

router = APIRouter(tags=["labels"])


@router.get("/labels", response_model=list[LabelResponse])
async def get_labels(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_labels(user, db)


@router.post("/labels", response_model=LabelResponse, status_code=status.HTTP_201_CREATED)
async def post_label(
    payload: LabelCreate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await create_label(user, payload, db)
    except LabelAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f'A label named "{payload.name}" already exists.',
        )


@router.patch("/labels/{label_id}", response_model=LabelResponse)
async def patch_label(
    label_id: int,
    payload: LabelUpdate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    label = await update_label(user, label_id, payload, db)
    if not label:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Label not found")
    return label


@router.delete("/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT)
async def del_label(
    label_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    if not await delete_label(user, label_id, db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Label not found")


@router.post("/articles/{article_id}/labels", status_code=status.HTTP_204_NO_CONTENT)
async def post_article_label(
    article_id: int,
    payload: ArticleLabelAssign,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    if not await assign_label(user, article_id, payload.label_id, db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Label not found")


@router.delete("/articles/{article_id}/labels/{label_id}", status_code=status.HTTP_204_NO_CONTENT)
async def del_article_label(
    article_id: int,
    label_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    if not await remove_label(user, article_id, label_id, db):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Label assignment not found"
        )

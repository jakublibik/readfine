from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.user import User
from app.schemas.filter import FilterCreate, FilterResponse, FilterTestResult, FilterUpdate
from app.services.filter_service import (
    apply_filter_retroactively,
    create_filter,
    delete_filter,
    get_filter,
    list_filters,
    test_filter,
    update_filter,
)

router = APIRouter(prefix="/filters", tags=["filters"])


@router.get("", response_model=list[FilterResponse])
async def get_filters(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_filters(user.id, db)


@router.post("", response_model=FilterResponse, status_code=status.HTTP_201_CREATED)
async def post_filter(
    payload: FilterCreate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await create_filter(user.id, payload, db)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))


@router.get("/{filter_id}", response_model=FilterResponse)
async def get_filter_detail(
    filter_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    f = await get_filter(user.id, filter_id, db)
    if not f:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filter not found")
    return f


@router.patch("/{filter_id}", response_model=FilterResponse)
async def patch_filter(
    filter_id: int,
    payload: FilterUpdate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        f = await update_filter(user.id, filter_id, payload, db)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))
    if not f:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filter not found")
    return f


@router.delete("/{filter_id}", status_code=status.HTTP_204_NO_CONTENT)
async def del_filter(
    filter_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    if not await delete_filter(user.id, filter_id, db):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filter not found")


@router.post("/{filter_id}/test", response_model=FilterTestResult)
async def post_filter_test(
    filter_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    result = await test_filter(user.id, filter_id, db)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filter not found")
    return result


@router.post("/{filter_id}/apply", response_model=dict)
async def post_filter_apply(
    filter_id: int,
    enqueue_scoring: bool = True,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    matched, changed, scoring_queued = await apply_filter_retroactively(
        user.id, filter_id, db, enqueue_scoring=enqueue_scoring
    )
    if matched == 0 and changed == 0:
        # Distinguish "not found" from "matched nothing" by checking existence
        f = await get_filter(user.id, filter_id, db)
        if not f:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Filter not found")
    return {"matched": matched, "changed": changed, "scoring_queued": scoring_queued}

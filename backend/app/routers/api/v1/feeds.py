from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.feed import Feed, Folder, UserFeed
from app.models.user import User
from app.schemas.feed import FeedSubscribeRequest, UserFeedResponse, UserFeedUpdate
from app.services.feed import list_user_feeds, subscribe, unsubscribe
from app.utils.crypto import encrypt

router = APIRouter(prefix="/feeds", tags=["feeds"])


@router.get("", response_model=list[UserFeedResponse])
async def get_feeds(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_user_feeds(user, db)


@router.post("", response_model=UserFeedResponse, status_code=status.HTTP_201_CREATED)
async def subscribe_feed(
    payload: FeedSubscribeRequest,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        user_feed = await subscribe(
            user=user,
            url=payload.url,
            folder_id=payload.folder_id,
            custom_title=payload.custom_title,
            fetch_auth_user=payload.fetch_auth_user,
            fetch_auth_pass=payload.fetch_auth_pass.get_secret_value() if payload.fetch_auth_pass else None,
            db=db,
        )
    except ValueError as e:
        status_code = status.HTTP_409_CONFLICT if "already subscribed" in str(e).lower() else status.HTTP_400_BAD_REQUEST
        raise HTTPException(status_code=status_code, detail=str(e))

    # Reload with feed relationship
    result = await db.execute(
        select(UserFeed).options(selectinload(UserFeed.feed)).where(UserFeed.id == user_feed.id)
    )
    return result.scalar_one()


@router.get("/{user_feed_id}", response_model=UserFeedResponse)
async def get_feed(
    user_feed_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserFeed)
        .options(selectinload(UserFeed.feed))
        .where(
            UserFeed.id == user_feed_id,
            UserFeed.user_id == user.id,
        )
    )
    user_feed = result.scalar_one_or_none()
    if not user_feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")
    return user_feed


@router.patch("/{user_feed_id}", response_model=UserFeedResponse)
async def update_feed(
    user_feed_id: int,
    payload: UserFeedUpdate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(UserFeed)
        .options(selectinload(UserFeed.feed))
        .where(
            UserFeed.id == user_feed_id,
            UserFeed.user_id == user.id,
        )
    )
    user_feed = result.scalar_one_or_none()
    if not user_feed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feed not found")

    if payload.custom_title is not None:
        user_feed.custom_title = payload.custom_title[:255] if payload.custom_title else None
    if "folder_id" in payload.model_fields_set:
        if payload.folder_id is not None:
            folder_result = await db.execute(
                select(Folder).where(Folder.id == payload.folder_id, Folder.user_id == user.id)
            )
            if not folder_result.scalar_one_or_none():
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
        user_feed.folder_id = payload.folder_id
    if payload.extract_readable is not None:
        user_feed.extract_readable = payload.extract_readable
    if payload.purge_after_days is not None:
        user_feed.purge_after_days = payload.purge_after_days
    if payload.purge_keep_count is not None:
        user_feed.purge_keep_count = payload.purge_keep_count
    if payload.position is not None:
        user_feed.position = payload.position

    auth_fields = {"fetch_auth_user", "fetch_auth_pass"} & payload.model_fields_set
    if auth_fields:
        feed = user_feed.feed
        if feed.is_private:
            if "fetch_auth_user" in payload.model_fields_set and not payload.fetch_auth_user:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fetch_auth_user cannot be empty for a private feed")
            if "fetch_auth_pass" in payload.model_fields_set and not payload.fetch_auth_pass:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fetch_auth_pass cannot be empty for a private feed")
        if "fetch_auth_user" in payload.model_fields_set:
            feed.fetch_auth_user = payload.fetch_auth_user
        if "fetch_auth_pass" in payload.model_fields_set and payload.fetch_auth_pass:
            feed.fetch_auth_pass_encrypted = encrypt(payload.fetch_auth_pass.get_secret_value())

    await db.commit()
    await db.refresh(user_feed)
    return user_feed


@router.delete("/{user_feed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_feed(
    user_feed_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        await unsubscribe(user, user_feed_id, db)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))

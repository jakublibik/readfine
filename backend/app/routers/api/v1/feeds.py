from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.feed import Feed, Folder, UserFeed
from app.models.user import User
from app.schemas.feed import FeedSubscribeRequest, UserFeedResponse, UserFeedUpdate
from app.services.feed import AlreadySubscribed, attach_unread_counts, list_user_feeds, subscribe, unsubscribe
from app.utils.crypto import encrypt

router = APIRouter(prefix="/feeds", tags=["feeds"])


@router.get("", response_model=list[UserFeedResponse])
async def get_feeds(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    return await list_user_feeds(user, db, include_unread=True)


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
    except AlreadySubscribed as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    # Reload with feed relationship
    result = await db.execute(
        select(UserFeed).options(selectinload(UserFeed.feed)).where(UserFeed.id == user_feed.id)
    )
    reloaded = result.scalar_one()
    await attach_unread_counts(user.id, [reloaded], db)
    return reloaded


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
    await attach_unread_counts(user.id, [user_feed], db)
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
        was_off = not user_feed.extract_readable
        user_feed.extract_readable = payload.extract_readable
        if payload.extract_readable and was_off:
            # Turning extraction back on restarts the auto-disable streaks; without it
            # the 403s that got the feed disabled are still its newest terminal
            # articles and the next one re-disables it on the spot.
            from app.services.readable_service import stamp_readable_streak_start
            await stamp_readable_streak_start(user_feed.feed_id, db)
    if payload.purge_after_days is not None:
        user_feed.purge_after_days = payload.purge_after_days
    if payload.purge_keep_count is not None:
        user_feed.purge_keep_count = payload.purge_keep_count
    if payload.position is not None:
        user_feed.position = payload.position

    auth_fields = {"fetch_auth_user", "fetch_auth_pass"} & payload.model_fields_set
    if auth_fields:
        feed = user_feed.feed
        if feed.status == "disabled":
            feed.status = "active"
        feed.fetch_error_count = 0
        feed.block_count = 0
        if not feed.is_private:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="HTTP auth credentials can only be set on private feeds",
            )
        # Count current subscribers to prevent one user from overwriting shared private feed auth
        sub_count_result = await db.execute(
            select(UserFeed).where(UserFeed.feed_id == feed.id)
        )
        subscriber_count = len(sub_count_result.scalars().all())
        if subscriber_count > 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="HTTP auth credentials cannot be changed while multiple users are subscribed to this feed",
            )
        if "fetch_auth_user" in payload.model_fields_set:
            if not payload.fetch_auth_user:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="fetch_auth_user cannot be empty")
            feed.fetch_auth_user = payload.fetch_auth_user
        if "fetch_auth_pass" in payload.model_fields_set and payload.fetch_auth_pass:
            feed.fetch_auth_pass_encrypted = encrypt(payload.fetch_auth_pass.get_secret_value())

    await db.commit()
    await db.refresh(user_feed)
    await attach_unread_counts(user.id, [user_feed], db)
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

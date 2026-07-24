"""Settings landing route."""
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.feed import UserFeed
from app.models.user import User

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
@router.get("/")
async def settings_index(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Smart landing: Stats once the user has feeds, otherwise Feeds to get started."""
    has_feed = await db.scalar(
        select(UserFeed.id).where(UserFeed.user_id == user.id).limit(1)
    )
    target = "/settings/stats" if has_feed else "/settings/feeds"
    return RedirectResponse(target, status_code=303)

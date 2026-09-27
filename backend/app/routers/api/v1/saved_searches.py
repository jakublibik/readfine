from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.user import User
from app.schemas.saved_search import SavedSearchResponse
from app.services.saved_search_service import list_saved_searches

router = APIRouter(tags=["saved searches"])


@router.get("/saved-searches", response_model=list[SavedSearchResponse])
async def get_saved_searches(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    """The reader's saved searches, alphabetical, as the sidebar lists them.

    Read-only: they are created and changed in the web app. To get what one lists,
    pass its ``id`` as ``view_id`` to ``GET /articles``.
    """
    return await list_saved_searches(db, user.id)

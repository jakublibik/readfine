from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.feed import Folder
from app.models.user import User
from app.schemas.feed import FolderCreate, FolderResponse, FolderUpdate
from app.services import folder_service
from app.services.folder_service import FolderAlreadyExistsError, get_folder

router = APIRouter(prefix="/folders", tags=["folders"])


@router.get("", response_model=list[FolderResponse])
async def list_folders(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    # Same order the web UI shows, so a client rendering a sidebar from this
    # does not contradict what the user arranged in settings.
    order = folder_service.folder_order_clause(await folder_service.get_folder_order(db, user.id))
    result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(*order)
    )
    return result.scalars().all()


@router.post("", response_model=FolderResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: FolderCreate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await folder_service.create_folder(db, user.id, payload.name, payload.position)
    except FolderAlreadyExistsError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Folder name already exists")


@router.patch("/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: int,
    payload: FolderUpdate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await get_folder(db, user.id, folder_id)
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    if payload.position is not None:
        folder.position = payload.position
    if payload.name is not None:
        try:
            await folder_service.rename_folder(db, folder, payload.name)  # commits the position too
        except FolderAlreadyExistsError:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Folder name already exists")
    else:
        await db.commit()
    await db.refresh(folder)
    return folder


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    folder = await get_folder(db, user.id, folder_id)
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
    await folder_service.delete_folder(db, folder)

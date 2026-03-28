from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_api_user
from app.database import get_db
from app.models.feed import Folder
from app.models.user import User
from app.schemas.feed import FolderCreate, FolderResponse, FolderUpdate

router = APIRouter(prefix="/folders", tags=["folders"])


@router.get("", response_model=list[FolderResponse])
async def list_folders(
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder)
        .where(Folder.user_id == user.id)
        .order_by(Folder.position, Folder.name)
    )
    return result.scalars().all()


@router.post("", response_model=FolderResponse, status_code=status.HTTP_201_CREATED)
async def create_folder(
    payload: FolderCreate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(Folder).where(Folder.user_id == user.id, Folder.name == payload.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Folder name already exists")

    folder = Folder(user_id=user.id, name=payload.name, position=payload.position)
    db.add(folder)
    await db.commit()
    await db.refresh(folder)
    return folder


@router.patch("/{folder_id}", response_model=FolderResponse)
async def update_folder(
    folder_id: int,
    payload: FolderUpdate,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    if payload.name is not None:
        # Check name uniqueness (ignore self)
        dup = await db.execute(
            select(Folder).where(
                Folder.user_id == user.id,
                Folder.name == payload.name,
                Folder.id != folder_id,
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Folder name already exists")
        folder.name = payload.name

    if payload.position is not None:
        folder.position = payload.position

    await db.commit()
    await db.refresh(folder)
    return folder


@router.delete("/{folder_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_folder(
    folder_id: int,
    user: User = Depends(get_api_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if not folder:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")

    await db.delete(folder)
    await db.commit()

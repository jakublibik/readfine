"""Web routes for folder CRUD in settings."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.feed import Folder
from app.models.user import User
from app.services.scope_cleanup import strip_scope_references
from app.templating import templates

from .common import _get_feeds_context

router = APIRouter(prefix="/settings", tags=["settings"])


@router.post("/folders", response_class=HTMLResponse)
async def settings_folder_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    name = form.get("name", "").strip()
    if name:
        existing = await db.execute(
            select(Folder).where(Folder.user_id == user.id, Folder.name == name)
        )
        if not existing.scalar_one_or_none():
            db.add(Folder(user_id=user.id, name=name))
            await db.commit()
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
        "with_folder_oob": True,
    })


@router.delete("/folders/{folder_id}", response_class=HTMLResponse)
async def settings_folder_delete(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    cleanup = None
    if folder:
        cleanup = await strip_scope_references(db, kind="folder", ref_id=folder_id, user_id=user.id)
        await db.delete(folder)
        await db.commit()
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
        "with_folder_oob": True,
        "scope_cleanup": cleanup,
    })


@router.get("/folders/{folder_id}/rename-form", response_class=HTMLResponse)
async def settings_folder_rename_form(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if not folder:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "settings/partials/folder_rename_form.html", {
        "folder": folder,
    })


@router.post("/folders/{folder_id}/rename", response_class=HTMLResponse)
async def settings_folder_rename(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    name = form.get("name", "").strip()
    result = await db.execute(
        select(Folder).where(Folder.id == folder_id, Folder.user_id == user.id)
    )
    folder = result.scalar_one_or_none()
    if folder and name:
        folder.name = name
        await db.commit()
    user_feeds, folders, article_counts = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        "user_feeds": user_feeds,
        "folders": folders,
        "article_counts": article_counts,
        "with_folder_oob": True,
    })

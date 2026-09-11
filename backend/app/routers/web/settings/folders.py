"""Web routes for folder CRUD in settings."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.feed import Folder
from app.models.user import User
from app.services.folder_service import (
    move_folder, next_folder_position, reset_folder_order, set_folder_order,
)
from app.services.scope_cleanup import strip_scope_references
from app.templating import templates

from .common import _get_feeds_context, _get_or_create_settings

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
            position = await next_folder_position(db, user.id)
            db.add(Folder(user_id=user.id, name=name, position=position))
            await db.commit()
    ctx = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        **ctx,
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
    ctx = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        **ctx,
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
    ctx = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        **ctx,
        "with_folder_oob": True,
    })


@router.post("/folders/{folder_id}/move", response_class=HTMLResponse)
async def settings_folder_move(
    folder_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Move a folder one step up or down in the manual order.

    A folder that is already at the end it was asked to move towards, or that is
    not the user's, just re-renders the list unchanged.
    """
    form = await request.form()
    settings = await _get_or_create_settings(user, db)
    try:
        await move_folder(db, settings, folder_id, form.get("dir", ""))
    except ValueError:
        return HTMLResponse("Cannot move that folder", status_code=400)
    ctx = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        **ctx,
        # The subscribe form's folder dropdown follows the same order, so it has
        # to be swapped along or it keeps showing the order from before the move.
        "with_folder_oob": True,
    })


@router.post("/folder-order", response_class=HTMLResponse)
async def settings_folder_order(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    settings = await _get_or_create_settings(user, db)
    try:
        await set_folder_order(db, settings, form.get("mode", ""))
    except ValueError:
        return HTMLResponse("Unknown folder order", status_code=400)
    ctx = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        **ctx,
        "with_folder_oob": True,
    })


@router.post("/folder-order/reset", response_class=HTMLResponse)
async def settings_folder_order_reset(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Discard the arrangement and put the folders back in alphabetical order."""
    settings = await _get_or_create_settings(user, db)
    await reset_folder_order(db, settings)
    ctx = await _get_feeds_context(user, db)
    return templates.TemplateResponse(request, "settings/partials/feeds_list.html", {
        **ctx,
        "with_folder_oob": True,
    })

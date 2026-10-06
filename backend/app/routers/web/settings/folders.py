"""Web routes for folder CRUD in settings."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.feed import FolderCreate
from app.services.folder_service import (
    FolderAlreadyExistsError, create_folder, delete_folder, get_folder, move_folder,
    rename_folder, reset_folder_order, set_folder_order,
)
from app.templating import templates
from app.utils.htmx import error_toast, validation_message

from .common import _get_feeds_context, _get_or_create_settings

router = APIRouter(prefix="/settings", tags=["settings"])


@router.post("/folders", response_class=HTMLResponse)
async def settings_folder_create(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    try:
        payload = FolderCreate(name=form.get("name", ""))
        await create_folder(db, user.id, payload.name)
    except ValidationError as exc:
        return error_toast(validation_message(exc))
    except FolderAlreadyExistsError:
        return error_toast(f'A folder named "{payload.name}" already exists.', 409)
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
    folder = await get_folder(db, user.id, folder_id)
    cleanup = await delete_folder(db, folder) if folder else None
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
    folder = await get_folder(db, user.id, folder_id)
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
    folder = await get_folder(db, user.id, folder_id)
    if folder:
        try:
            payload = FolderCreate(name=form.get("name", ""))
            await rename_folder(db, folder, payload.name)
        except ValidationError as exc:
            return error_toast(validation_message(exc))
        except FolderAlreadyExistsError:
            return error_toast(f'A folder named "{payload.name}" already exists.', 409)
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

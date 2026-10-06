"""Web routes for label CRUD in settings."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.schemas.label import LabelCreate, LabelUpdate
from app.services.label_service import (
    LabelAlreadyExistsError, create_label, delete_label, list_labels, update_label,
)
from app.templating import templates
from app.utils.htmx import validation_message

router = APIRouter(prefix="/settings", tags=["settings"])


async def _labels_list(request: Request, user: User, db: AsyncSession, **extra) -> HTMLResponse:
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
        "labels": labels,
        **extra,
    })


@router.get("/labels", response_class=HTMLResponse)
async def settings_labels(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/labels.html", {"labels": labels})


@router.post("/labels", response_class=HTMLResponse)
async def settings_labels_create(
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = LabelCreate(name=name, color=color)
        await create_label(user, payload, db)
    except ValidationError as exc:
        return await _labels_list(request, user, db, error=validation_message(exc))
    except LabelAlreadyExistsError:
        return await _labels_list(
            request, user, db, error=f'A label named "{payload.name}" already exists.')
    return await _labels_list(request, user, db, success=f'Label "{payload.name}" added.')


@router.post("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_update(
    label_id: int,
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = LabelUpdate(name=name, color=color)
        await update_label(user, label_id, payload, db)
    except ValidationError as exc:
        return await _labels_list(request, user, db, error=validation_message(exc))
    except LabelAlreadyExistsError:
        return await _labels_list(
            request, user, db, error=f'A label named "{payload.name}" already exists.')
    return await _labels_list(request, user, db)


@router.delete("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_delete(
    label_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_label(user, label_id, db)
    return await _labels_list(request, user, db)

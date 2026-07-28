"""Web routes for label CRUD in settings."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.label import Label
from app.models.user import User
from app.schemas.label import LabelCreate, LabelUpdate
from app.services.label_service import create_label, delete_label, list_labels, update_label
from app.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])


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
    name = name.strip()
    existing = await db.scalar(select(Label).where(Label.user_id == user.id, Label.name == name))
    if existing:
        labels = await list_labels(user, db)
        return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
            "labels": labels,
            "error": f'A label named "{name}" already exists.',
        })
    await create_label(user, LabelCreate(name=name, color=color), db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
        "labels": labels,
        "success": f'Label "{name}" added.',
    })


@router.post("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_update(
    label_id: int,
    request: Request,
    name: str = Form(...),
    color: str = Form("#6366f1"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = name.strip()
    conflict = await db.scalar(
        select(Label).where(Label.user_id == user.id, Label.name == name, Label.id != label_id)
    )
    if conflict:
        labels = await list_labels(user, db)
        return templates.TemplateResponse(request, "settings/partials/labels_list.html", {
            "labels": labels,
            "error": f'A label named "{name}" already exists.',
        })
    await update_label(user, label_id, LabelUpdate(name=name, color=color), db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})


@router.delete("/labels/{label_id}", response_class=HTMLResponse)
async def settings_label_delete(
    label_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await delete_label(user, label_id, db)
    labels = await list_labels(user, db)
    return templates.TemplateResponse(request, "settings/partials/labels_list.html", {"labels": labels})

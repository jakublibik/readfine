"""Web routes for OPML import/export in settings."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.opml import MAX_UPLOAD_BYTES, export_opml, import_opml
from app.templating import templates

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/opml", response_class=HTMLResponse)
async def settings_opml(
    request: Request,
    user: User = Depends(get_current_user),
):
    return templates.TemplateResponse(request, "settings/opml.html", {})


@router.get("/opml/export")
async def settings_opml_export(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    xml = await export_opml(user, db)
    filename = f"readfine-{datetime.now(timezone.utc).strftime('%Y%m%d')}.opml"
    return Response(
        content=xml.encode("utf-8"),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/opml/import", response_class=HTMLResponse)
async def settings_opml_import(
    request: Request,
    file: UploadFile = File(...),
    import_feeds: bool = Form(False),
    import_labels: bool = Form(False),
    import_prefs: bool = Form(False),
    import_filters: bool = Form(False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        return templates.TemplateResponse(request, "settings/opml.html", {
            "error": "File too large (max 1 MB).",
        })

    try:
        result = await import_opml(
            user=user,
            xml_bytes=raw,
            import_feeds=import_feeds,
            import_labels=import_labels,
            import_prefs=import_prefs,
            import_filters=import_filters,
            db=db,
        )
    except ValueError as exc:
        return templates.TemplateResponse(request, "settings/opml.html", {
            "error": str(exc),
        })

    return templates.TemplateResponse(request, "settings/opml.html", {
        "import_result": result,
    })

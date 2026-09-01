from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.admin_service import get_app_settings
from app.templating import templates

router = APIRouter(tags=["legal"])


async def _legal_ctx(db: AsyncSession) -> dict:
    s = await get_app_settings(db)
    return {
        "legal_operator_name": s.legal_operator_name or "",
        "legal_contact_email": s.legal_contact_email or "",
        "legal_jurisdiction": s.legal_jurisdiction or "",
        "legal_last_updated": s.legal_last_updated or "",
    }


@router.get("/terms", response_class=HTMLResponse)
async def terms_of_service(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _legal_ctx(db) | {"footer_current": "terms"}
    return templates.TemplateResponse(request, "legal/terms.html", ctx)


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy(request: Request, db: AsyncSession = Depends(get_db)):
    ctx = await _legal_ctx(db) | {"footer_current": "privacy"}
    return templates.TemplateResponse(request, "legal/privacy.html", ctx)

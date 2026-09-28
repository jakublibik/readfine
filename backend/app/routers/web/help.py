from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.templating import templates
from app.utils.changelog import load_changelog
from app.utils.features import load_features

router = APIRouter(tags=["help"])


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Public getting-started guide + FAQ. Linked from the app, login and landing."""
    return templates.TemplateResponse(request, "help.html", {})


@router.get("/features", response_class=HTMLResponse)
async def features_page(request: Request):
    """Public, categorized feature list. Rendered from app/content/features.yml."""
    return templates.TemplateResponse(request, "features.html", {"features": load_features()})


@router.get("/changelog", response_class=HTMLResponse)
async def changelog_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Public release notes of the running code. Upgrade notes are for admins only."""
    is_admin = False
    if request.session.get("user_id"):
        try:
            user = await get_current_user(request, None, db)
            is_admin = user.role == "admin"
        except HTTPException:  # stale session: shown as signed out
            pass
    return templates.TemplateResponse(request, "changelog.html", {
        "releases": load_changelog(),
        "is_admin": is_admin,
    })

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates
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

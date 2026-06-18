from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import templates

router = APIRouter(tags=["help"])


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Public getting-started guide + FAQ. Linked from the app, login and landing."""
    return templates.TemplateResponse(request, "help.html", {})

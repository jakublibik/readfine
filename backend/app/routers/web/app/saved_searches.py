"""Saving, updating and deleting searches.

Saving leaves the reader where they are: in the search results, which keep
behaving as search results. The saved search behaves like a feed only once it is
opened from the sidebar. So these routes answer with an event, not a list, and
the client updates the header in place. A mistake the reader can fix (a taken
name, no filters) goes back onto the form's error line instead.
"""
import json
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from markupsafe import escape
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database import get_db
from app.models.saved_search import SavedSearch
from app.models.user import User
from app.services.saved_search_service import (
    SavedSearchError,
    create_saved_search,
    delete_saved_search,
    get_saved_search,
    mark_saved_search_read,
    update_saved_search,
)

router = APIRouter(tags=["web-app"])

# The form fields that carry the search itself, by their list-endpoint names.
_PARAM_FIELDS = (
    "q", "sort", "read_status", "scope_include", "label_filter",
    "score_source", "score_op", "score_val", "since_days", "state",
)


# Where a form shows its error: the edit form in the search window, or the name
# field that opens under the results header.
_ERROR_TARGETS = ("saved-search-error", "header-save-error")


async def _form(request: Request) -> tuple[str | None, dict, str]:
    """The name (None when the form doesn't send one), the search parameters and
    the element to put an error on."""
    form = await request.form()
    name = form.get("name")
    params = {k: form.get(k) for k in _PARAM_FIELDS if form.get(k) not in (None, "")}
    target = form.get("error_target")
    target = target if target in _ERROR_TARGETS else _ERROR_TARGETS[0]
    return (str(name) if name is not None else None), params, target


def _error(msg: str, target: str) -> HTMLResponse:
    resp = HTMLResponse(str(escape(msg)))
    resp.headers["HX-Retarget"] = f"#{target}"
    resp.headers["HX-Reswap"] = "innerHTML"
    return resp


def _saved(saved: SavedSearch, toast: str) -> HTMLResponse:
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({
        "showToast": {"msg": toast, "type": "ok"},
        # The client marks the results header as saved; the sidebar reloads its list.
        "savedSearchesChanged": {"id": saved.id, "name": saved.name},
    })})


async def _own(db: AsyncSession, user: User, search_id: int) -> SavedSearch:
    saved = await get_saved_search(db, user.id, search_id)
    if saved is None:
        raise HTTPException(status_code=404)
    return saved


@router.post("/htmx/saved-searches", response_class=HTMLResponse)
async def htmx_create_saved_search(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name, params, target = await _form(request)
    try:
        saved = await create_saved_search(db, user.id, name=name or "", params=params)
    except SavedSearchError as exc:
        await db.rollback()
        return _error(str(exc), target)
    await db.commit()
    return _saved(saved, f"Saved “{saved.name}”")


@router.post("/htmx/saved-searches/{search_id}", response_class=HTMLResponse)
async def htmx_update_saved_search(
    search_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    saved = await _own(db, user, search_id)
    name, params, target = await _form(request)
    try:
        await update_saved_search(db, saved, name=name, params=params)
    except SavedSearchError as exc:
        await db.rollback()
        return _error(str(exc), target)
    await db.commit()
    return _saved(saved, f"Updated “{saved.name}”")


@router.post("/htmx/saved-searches/{search_id}/delete", response_class=HTMLResponse)
async def htmx_delete_saved_search(
    search_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    saved = await _own(db, user, search_id)
    name = saved.name
    await delete_saved_search(db, saved)
    await db.commit()
    return HTMLResponse("", headers={"HX-Trigger": json.dumps({
        "showToast": {"msg": f"Deleted “{name}”", "type": "ok"},
        "savedSearchDeleted": {"id": search_id},
    })})


@router.post("/htmx/saved-searches/{search_id}/mark-read", response_class=HTMLResponse)
async def htmx_mark_saved_search_read(
    search_id: int,
    before: str = Form(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The sidebar's mark-all-read for a saved search, over exactly what it lists."""
    saved = await _own(db, user, search_id)
    try:
        before_dt = datetime.fromisoformat(before.replace("Z", "+00:00"))
    except ValueError:
        return HTMLResponse("", status_code=400)
    await mark_saved_search_read(db, user, saved, before=before_dt)
    await db.commit()
    return HTMLResponse("", headers={"HX-Trigger": "sidebarRefresh"})

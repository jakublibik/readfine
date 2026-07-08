"""Cleanup of dangling feed/folder references in filter & catchup/briefing scopes.

Scope is stored as JSON arrays of ``"feed:<Feed.id>"`` / ``"folder:<Folder.id>"``
tokens in three columns: ``Filter.scope_include``, ``Filter.scope_except`` and
``UserCatchupConfig.scope_include`` (the latter backs both Catch-me-up and
briefings). Deleting a feed subscription or a folder would otherwise leave those
tokens dangling.

Two cases would silently *widen* a scope and so are handled specially, with a
user-facing report:

* a ``Filter`` whose ``scope_include`` is emptied by the strip would fall back to
  "all feeds" (empty = all) → the filter is **deactivated** instead;
* a briefing-enabled ``UserCatchupConfig`` whose ``scope_include`` is emptied
  would widen the emailed digest to every feed (a real cost/volume blow-up) →
  the **briefing is disabled**.

A plain catch-up config (no briefing) is left with the emptied scope = all feeds,
which is consistent with its own default and carries no cost surprise.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.filter import Filter
from app.models.user import UserCatchupConfig


@dataclass
class ScopeCleanupResult:
    """Names of items whose behaviour changed and should be surfaced to the user."""

    deactivated_filters: list[str] = field(default_factory=list)
    disabled_briefings: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.deactivated_filters or self.disabled_briefings)


def _strip(value: str | None, token: str) -> tuple[str | None, bool, bool]:
    """Remove ``token`` from a JSON scope list.

    Returns ``(new_value, was_present, emptied)`` where ``new_value`` is the
    re-serialized JSON (or ``None`` when empty/absent), ``was_present`` is whether
    the token appeared, and ``emptied`` is whether a non-empty list became empty
    as a result of the strip. Corrupt JSON is left untouched.
    """
    if not value:
        return value, False, False
    try:
        items = [i for i in json.loads(value) if isinstance(i, str)]
    except (json.JSONDecodeError, TypeError):
        return value, False, False
    if token not in items:
        return value, False, False
    remaining = [i for i in items if i != token]
    return (json.dumps(remaining) if remaining else None), True, not remaining


async def strip_scope_references(
    db: AsyncSession,
    *,
    kind: str,
    ref_id: int,
    user_id: int | None,
) -> ScopeCleanupResult:
    """Strip ``feed:<id>`` / ``folder:<id>`` refs from filter & catchup scopes.

    ``kind`` is ``"feed"`` or ``"folder"``. ``user_id`` scopes the cleanup to one
    user (self-service unsubscribe / folder delete); ``None`` cleans across all
    users (admin feed delete — the shared ``Feed`` is gone for everyone). Mutates
    the ORM objects in the session; the caller owns the commit.
    """
    token = f"{kind}:{ref_id}"
    like = f'%"{token}"%'
    result = ScopeCleanupResult()

    fq = select(Filter).where(
        Filter.scope_include.like(like) | Filter.scope_except.like(like)
    )
    if user_id is not None:
        fq = fq.where(Filter.user_id == user_id)
    for f in (await db.execute(fq)).scalars():
        new_inc, inc_present, inc_emptied = _strip(f.scope_include, token)
        new_exc, exc_present, _ = _strip(f.scope_except, token)
        if inc_present:
            f.scope_include = new_inc
        if exc_present:
            f.scope_except = new_exc
        if inc_emptied and f.is_active:
            f.is_active = False
            result.deactivated_filters.append(f.name)

    cq = select(UserCatchupConfig).where(UserCatchupConfig.scope_include.like(like))
    if user_id is not None:
        cq = cq.where(UserCatchupConfig.user_id == user_id)
    for c in (await db.execute(cq)).scalars():
        new_inc, present, emptied = _strip(c.scope_include, token)
        if present:
            c.scope_include = new_inc
        if emptied and c.briefing_enabled:
            c.briefing_enabled = False
            c.briefing_next_send_at = None
            result.disabled_briefings.append(c.name)

    return result

"""The search's parameters: what each knob accepts, in one place.

The search modal submits them as a query string, a saved search keeps them in
JSONB, and both have to mean the same list. The router cleans its query string
with `search_score` / `search_state`, and `normalize_search_params` builds on
those to clean a set that is about to be stored, so a saved search can't hold a
value the ad-hoc search would read differently.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.services.article import MAX_QUERY_LENGTH, SCORE_SOURCES

# The search's List filter: one of the reader's own lists, as the sidebar names them.
SEARCH_STATES = ("starred", "saved", "archived")

# The search's sort selector. "relevance" (ts_rank) is the default and only means
# something with a query term; without one list_articles treats it as newest.
SEARCH_SORTS = ("relevance", "newest", "oldest", "score")

# The status selector. "All" is the absence of one.
READ_STATUSES = ("unread", "read", "engaged", "not_engaged")

# Same bounds as the list endpoints' Query(...) declarations.
SINCE_DAYS_MAX = 3650


def search_score(
    score_source: str | None, score_op: str | None, score_val: float | None,
    sort: str | None = None,
) -> dict:
    """The search's score knobs, cleaned up, keyed as ``list_articles`` takes them.

    Holds a condition (source, operator, value) when one is set, or only the source
    when the results are sorted by score without one. Empty means the search does
    nothing with scores. The source falls back to AI, else basic, the number the
    list shows.
    """
    src = score_source if score_source in SCORE_SOURCES else "relevance"
    if score_op in ("gte", "lt") and score_val is not None:
        return {"score_source": src, "score_op": score_op, "score_val": score_val}
    if sort == "score":
        return {"score_source": src}
    return {}


def search_state(state: str | None) -> str | None:
    """The List filter, or None for any article."""
    return state if state in SEARCH_STATES else None


def _token_list(value: Any) -> list:
    """A multi-select scope as a list, whether it arrives as the query string's JSON
    text or already as a list. Anything unreadable is no selection."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else []
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def _id_token(item: Any, prefix: str) -> str | None:
    """``item`` back as ``"<prefix><int>"`` when it is one, else None."""
    if not isinstance(item, str) or not item.startswith(prefix):
        return None
    try:
        n = int(item[len(prefix):])
    except ValueError:
        return None
    return f"{prefix}{n}" if n >= 0 else None


def clean_scope(value: Any) -> list[str]:
    """``feed:<id>`` / ``folder:<id>`` tokens (``folder:0`` = no folder), deduplicated
    in their original order."""
    out: list[str] = []
    for item in _token_list(value):
        token = _id_token(item, "feed:") or _id_token(item, "folder:")
        if token and token not in out:
            out.append(token)
    return out


def clean_labels(value: Any) -> list[str]:
    """``["any"]`` (has any label) or ``label:<id>`` tokens. "any" wins, as it does in
    `scope_tokens.parse_label_tokens`."""
    items = _token_list(value)
    if "any" in items:
        return ["any"]
    out: list[str] = []
    for item in items:
        token = _id_token(item, "label:")
        if token and token not in out:
            out.append(token)
    return out


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):  # a stored or imported Infinity
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_search_params(raw: Mapping[str, Any]) -> dict:
    """The search parameters worth keeping, cleaned, from the modal's query-string keys.

    Unknown keys and values outside what the search offers are dropped rather than
    rejected, the way the list endpoint ignores them, so what is stored is exactly
    what the list would have used. Defaults are left out: no key means the search's
    own default (any status, any list, all time, relevance order).
    """
    params: dict = {}

    q = raw.get("q")
    if isinstance(q, str) and q.strip():
        params["q"] = q.strip()[:MAX_QUERY_LENGTH]

    sort = raw.get("sort")
    if sort in SEARCH_SORTS and sort != "relevance":
        params["sort"] = sort

    if raw.get("read_status") in READ_STATUSES:
        params["read_status"] = raw["read_status"]

    scope = clean_scope(raw.get("scope_include"))
    if scope:
        params["scope_include"] = scope

    labels = clean_labels(raw.get("label_filter"))
    if labels:
        params["label_filter"] = labels

    score_val = _float_or_none(raw.get("score_val"))
    if score_val is not None and not 0 <= score_val <= 100:
        score_val = None
    params.update(search_score(
        raw.get("score_source"), raw.get("score_op"), score_val, params.get("sort"),
    ))

    since_days = _int_or_none(raw.get("since_days"))
    if since_days is not None and 1 <= since_days <= SINCE_DAYS_MAX:
        params["since_days"] = since_days

    state = search_state(raw.get("state"))
    if state:
        params["state"] = state

    return params


def list_kwargs(params: Mapping[str, Any]) -> dict:
    """A stored parameter set as the list renderer takes it: the scopes back as the
    JSON text the query string carries, the score knobs folded into ``score``."""
    return {
        "q": params.get("q"),
        "sort": params.get("sort", "relevance"),
        "read_status": params.get("read_status"),
        "scope_include": json.dumps(params["scope_include"]) if params.get("scope_include") else None,
        "label_filter": json.dumps(params["label_filter"]) if params.get("label_filter") else None,
        "score": search_score(
            params.get("score_source"), params.get("score_op"), params.get("score_val"),
            params.get("sort"),
        ),
        "since_days": params.get("since_days"),
        "state": params.get("state"),
    }


def modal_values(params: Mapping[str, Any]) -> dict[str, str]:
    """A stored parameter set in the search modal's own vocabulary: the names its
    prefill query string and the client's remembered search use, all as strings.

    The sort says what the list actually used, as the modal does on submit: without
    a query term relevance means newest.
    """
    score_op = params.get("score_op")
    return {
        "q": params.get("q", ""),
        "scope": json.dumps(params["scope_include"]) if params.get("scope_include") else "",
        "sort": params.get("sort") or ("relevance" if params.get("q") else "newest"),
        "status": params.get("read_status", ""),
        "labels": json.dumps(params["label_filter"]) if params.get("label_filter") else "",
        "score_source": params.get("score_source", "") if score_op else "",
        "score_op": score_op or "",
        "score_val": f"{params['score_val']:g}" if score_op else "",
        "since_days": str(params.get("since_days", "")),
        "state": params.get("state", ""),
    }

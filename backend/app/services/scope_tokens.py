"""Parsing and matching of scope / label selector tokens.

The UI stores multi-select scopes as JSON arrays of opaque string tokens, shared by
filters, catch-up/briefing configs and the article list / search:

* ``"feed:<Feed.id>"``     — a specific feed
* ``"folder:<Folder.id>"`` — a folder; ``folder:0`` is the sentinel for "no folder"
* ``"label:<Label.id>"``   — a label (label selector only); ``"any"`` = has any label

Keeping the vocabulary in one place means the token grammar (and the ``0`` / ``any``
sentinels) is defined once instead of re-parsed in every consumer.
"""
import json


_INT_MAX = 2**31 - 1


def token_id(raw: str) -> int:
    """The id in a token (``feed:<id>`` minus its prefix).

    Raises ValueError for anything but a number that fits the Integer id
    columns: one past them would fail in the database query that uses it.
    """
    value = int(raw)
    if not 0 <= value <= _INT_MAX:
        raise ValueError(f"Invalid id in token: {raw}")
    return value


def _json_list(raw: str | None) -> list:
    """*raw* parsed as a JSON array, or [] for anything else.

    The value comes from a form or a query string, so ``5`` or ``{}`` is as
    likely as a list and must not reach the loops below.
    """
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return items if isinstance(items, list) else []


def parse_scope_tokens(scope_json: str | None) -> tuple[list[int], list[int]]:
    """Return ``(feed_ids, folder_ids)`` from a JSON scope list.

    ``folder_id`` 0 is kept as-is (the "no folder" sentinel — callers handle it).
    Empty, null or invalid JSON yields ``([], [])``; malformed items are skipped.
    """
    items = _json_list(scope_json)

    feed_ids: list[int] = []
    folder_ids: list[int] = []
    for item in items:
        try:
            if item.startswith("feed:"):
                feed_ids.append(token_id(item[5:]))
            elif item.startswith("folder:"):
                folder_ids.append(token_id(item[7:]))
        except (ValueError, IndexError, AttributeError):
            pass
    return feed_ids, folder_ids


def token_matches_article(item: str, article, user_feed) -> bool:
    """True if a single ``feed:``/``folder:`` token matches this article.

    ``folder:0`` matches articles in feeds with no folder. Needs *user_feed* (the
    subscriber's row, or None) to resolve folder membership.
    """
    try:
        if item.startswith("feed:"):
            return article.feed_id == int(item[5:])
        if item.startswith("folder:"):
            folder_val = int(item[7:])
            if folder_val == 0:  # sentinel: feeds with no folder
                return user_feed is not None and user_feed.folder_id is None
            return user_feed is not None and user_feed.folder_id == folder_val
    except (ValueError, IndexError):
        pass
    return False


def parse_label_tokens(label_json: str | None) -> tuple[bool, list[int]]:
    """Parse a label-filter JSON list into ``(any_label, label_ids)``.

    ``"any"`` ("has at least one label") takes precedence over specific ids.
    Empty or invalid input means no label filtering: ``(False, [])``.
    """
    items = _json_list(label_json)
    if "any" in items:
        return True, []
    ids: list[int] = []
    for item in items:
        if isinstance(item, str) and item.startswith("label:"):
            try:
                ids.append(token_id(item[6:]))
            except ValueError:
                pass
    return False, ids

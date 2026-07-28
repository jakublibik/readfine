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


def parse_scope_tokens(scope_json: str | None) -> tuple[list[int], list[int]]:
    """Return ``(feed_ids, folder_ids)`` from a JSON scope list.

    ``folder_id`` 0 is kept as-is (the "no folder" sentinel — callers handle it).
    Empty, null or invalid JSON yields ``([], [])``; malformed items are skipped.
    """
    if not scope_json:
        return [], []
    try:
        items = json.loads(scope_json)
    except (json.JSONDecodeError, TypeError):
        return [], []

    feed_ids: list[int] = []
    folder_ids: list[int] = []
    for item in items:
        try:
            if item.startswith("feed:"):
                feed_ids.append(int(item[5:]))
            elif item.startswith("folder:"):
                folder_ids.append(int(item[7:]))
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
    if not label_json:
        return False, []
    try:
        items = json.loads(label_json)
    except (json.JSONDecodeError, TypeError):
        return False, []
    if "any" in items:
        return True, []
    ids: list[int] = []
    for item in items:
        if isinstance(item, str) and item.startswith("label:"):
            try:
                ids.append(int(item[6:]))
            except ValueError:
                pass
    return False, ids

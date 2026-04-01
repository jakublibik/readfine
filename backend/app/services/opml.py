"""OPML import and export service."""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import defusedxml.ElementTree as _safe_ET
from xml.etree.ElementTree import Element, SubElement, indent, tostring

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.feed import Feed, Folder, UserFeed
from app.models.filter import Filter, FilterAction, FilterCondition
from app.models.label import Label
from app.models.user import User, UserSettings
from app.schemas.filter import FilterConditionCreate, FilterActionCreate, FilterCreate
from app.services.feed import subscribe
from app.services.filter_service import create_filter

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1 * 1024 * 1024  # 1 MB


class _FeedLimitReached(Exception):
    """Raised when the user's feed subscription limit is hit during OPML import."""

# ── TTRSS filter_type / action_id mappings ────────────────────────────────────

_TTRSS_FIELD_MAP = {
    "1": "title",
    "3": "title_or_content",
    "4": "url",
    "5": "content",
    "6": "author",
}

_TTRSS_ACTION_MAP = {
    "2": "mark_read",
    "3": "hide",
    "4": "star",
    "7": "label",
}

_REGEX_SPECIAL = re.compile(r"[.*+?^${}()|[\]\\]")


def _looks_like_regex(value: str) -> bool:
    return bool(_REGEX_SPECIAL.search(value))


# ── Export ────────────────────────────────────────────────────────────────────

async def export_opml(user: User, db: AsyncSession) -> str:
    """Build and return an OPML 2.0 XML string for the user's subscriptions."""

    # Load data
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(func.lower(Folder.name))
    )
    folders = {f.id: f for f in folders_result.scalars()}

    feeds_result = await db.execute(
        select(UserFeed, Feed)
        .join(Feed, Feed.id == UserFeed.feed_id)
        .outerjoin(Folder, Folder.id == UserFeed.folder_id)
        .where(UserFeed.user_id == user.id)
        .order_by(
            func.lower(Folder.name).nullsfirst(),
            func.lower(func.coalesce(UserFeed.custom_title, Feed.title)),
        )
    )
    user_feeds = feeds_result.all()

    labels_result = await db.execute(
        select(Label).where(Label.user_id == user.id).order_by(Label.position, Label.name)
    )
    labels = labels_result.scalars().all()

    filters_result = await db.execute(
        select(Filter)
        .where(Filter.user_id == user.id)
        .options(selectinload(Filter.conditions), selectinload(Filter.actions))
        .order_by(Filter.position)
    )
    filters = filters_result.scalars().all()

    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    user_settings = settings_result.scalar_one_or_none()

    # Lookup maps for scope export
    feed_id_to_url: dict[int, str] = {}
    folder_id_to_name: dict[int, str] = {}
    for uf, feed in user_feeds:
        feed_id_to_url[feed.id] = feed.feed_url
    for folder in folders.values():
        folder_id_to_name[folder.id] = folder.name

    # Build XML
    root = Element("opml", version="2.0")
    head = SubElement(root, "head")
    SubElement(head, "title").text = "Filtread subscriptions"
    SubElement(head, "dateCreated").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )

    body = SubElement(root, "body")

    # Group feeds by folder
    by_folder: dict[int | None, list[tuple[UserFeed, Feed]]] = {}
    for uf, feed in user_feeds:
        by_folder.setdefault(uf.folder_id, []).append((uf, feed))

    # Feeds without a folder first
    for uf, feed in by_folder.get(None, []):
        _feed_outline(body, uf, feed)

    # Feeds inside folders
    for folder_id, folder in folders.items():
        if folder_id not in by_folder:
            continue
        folder_el = SubElement(body, "outline", text=folder.name, title=folder.name)
        for uf, feed in by_folder[folder_id]:
            _feed_outline(folder_el, uf, feed)

    # Labels section
    if labels:
        labels_el = SubElement(body, "outline", text="tt-rss-labels")
        for label in labels:
            SubElement(
                labels_el,
                "outline",
                text=f"-{label.name}",
                **{"label-name": label.name, "label-bg-color": label.color},
            )

    # Prefs section
    prefs_data: list[tuple[str, str]] = []
    if user_settings:
        if user_settings.timezone:
            prefs_data.append(("USER_TIMEZONE", user_settings.timezone))
    if prefs_data:
        prefs_el = SubElement(body, "outline", text="tt-rss-prefs")
        for key, value in prefs_data:
            SubElement(prefs_el, "outline", text=key, value=value)

    # Filters section
    if filters:
        filters_payload = []
        for f in filters:
            scope_include_urls = _scope_to_urls(f.scope_include, feed_id_to_url, folder_id_to_name)
            scope_except_urls = _scope_to_urls(f.scope_except, feed_id_to_url, folder_id_to_name)
            filters_payload.append({
                "name": f.name,
                "enabled": f.is_active,
                "match_operator": f.match_operator,
                "stop_on_match": f.stop_on_match,
                "scope_include": scope_include_urls,
                "scope_except": scope_except_urls,
                "conditions": [
                    {
                        "field": c.field,
                        "operator": c.operator,
                        "value": c.value,
                        "position": c.position,
                    }
                    for c in sorted(f.conditions, key=lambda x: x.position)
                ],
                "actions": [
                    {
                        "action_type": a.action_type,
                        "action_value": a.action_value,
                    }
                    for a in f.actions
                ],
            })

        filters_el = SubElement(body, "outline", text="tt-rss-filters")
        filters_el.text = json.dumps(filters_payload, ensure_ascii=False)

    indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode")


def _feed_outline(parent: Element, uf: UserFeed, feed: Feed) -> None:
    title = uf.custom_title or feed.title or feed.feed_url
    attrs: dict[str, str] = {
        "text": title,
        "title": title,
        "type": "rss",
        "xmlUrl": feed.feed_url,
    }
    if feed.site_url:
        attrs["htmlUrl"] = feed.site_url
    SubElement(parent, "outline", **attrs)


def _scope_to_urls(
    scope_json: str | None,
    feed_id_to_url: dict[int, str],
    folder_id_to_name: dict[int, str],
) -> list[str]:
    if not scope_json:
        return []
    try:
        items = json.loads(scope_json)
    except (json.JSONDecodeError, TypeError):
        return []
    result = []
    for item in items:
        if item.startswith("feed:"):
            feed_id = int(item[5:])
            url = feed_id_to_url.get(feed_id)
            if url:
                result.append(f"feed:{url}")
        elif item.startswith("folder:"):
            folder_id_str = item[7:]
            if folder_id_str == "0":
                result.append("folder:__no_folder__")
            else:
                folder_id = int(folder_id_str)
                name = folder_id_to_name.get(folder_id)
                if name:
                    result.append(f"folder:{name}")
    return result


# ── Import ────────────────────────────────────────────────────────────────────

@dataclass
class ImportResult:
    feeds_added: int = 0
    feeds_skipped: int = 0
    feeds_failed: int = 0
    labels_added: int = 0
    labels_skipped: int = 0
    prefs_updated: int = 0
    filters_added: int = 0
    filters_skipped: int = 0
    warnings: list[str] = field(default_factory=list)


async def import_opml(
    user: User,
    xml_bytes: bytes,
    import_feeds: bool,
    import_labels: bool,
    import_prefs: bool,
    import_filters: bool,
    db: AsyncSession,
) -> ImportResult:
    result = ImportResult()

    try:
        root = _safe_ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
    except _safe_ET.ParseError as exc:
        raise ValueError(f"Invalid OPML file: {exc}") from exc

    body = root.find("body")
    if body is None:
        raise ValueError("OPML file has no <body> element")

    # Pass 1: import labels first (needed for filter action_value resolution)
    label_name_to_id: dict[str, int] = {}

    if import_labels or import_filters:
        labels_el = _find_section(body, "tt-rss-labels")
        if labels_el is not None:
            for outline in labels_el:
                label_name = outline.get("label-name") or outline.get("text", "").lstrip("-")
                color = outline.get("label-bg-color") or outline.get("label-fg-color") or "#6366f1"
                if not label_name:
                    continue
                label_name = label_name[:100]
                existing = await db.execute(
                    select(Label).where(Label.user_id == user.id, Label.name == label_name)
                )
                label = existing.scalar_one_or_none()
                if label:
                    label_name_to_id[label_name] = label.id
                    if import_labels:
                        result.labels_skipped += 1
                else:
                    if import_labels:
                        new_label = Label(user_id=user.id, name=label_name, color=color[:7])
                        db.add(new_label)
                        await db.flush()
                        label_name_to_id[label_name] = new_label.id
                        result.labels_added += 1
        await db.commit()

    # Pass 2: import feeds + folders
    feed_url_to_id: dict[str, int] = {}
    folder_name_to_id: dict[str, int] = {}

    if import_feeds:
        # Collect all top-level feed outlines, unwrapping TTRSS "All articles" wrapper
        feed_outlines = _collect_feed_outlines(body)
        for outline, folder_name in feed_outlines:
            folder_id = None
            if folder_name:
                folder_id = await _get_or_create_folder(user, folder_name, folder_name_to_id, db)
            xml_url = outline.get("xmlUrl", "")
            try:
                added_id = await _import_feed(user, outline, folder_id, result, db)
            except _FeedLimitReached:
                result.warnings.append("Feed limit reached — remaining feeds skipped")
                break
            if added_id and xml_url:
                feed_url_to_id[xml_url] = added_id

        # Refresh existing subscriptions into lookup map
        existing_uf_result = await db.execute(
            select(UserFeed, Feed)
            .join(Feed, Feed.id == UserFeed.feed_id)
            .where(UserFeed.user_id == user.id)
        )
        for uf, feed in existing_uf_result.all():
            if feed.feed_url not in feed_url_to_id:
                feed_url_to_id[feed.feed_url] = uf.feed_id

        # Refresh folder map
        existing_folders = await db.execute(
            select(Folder).where(Folder.user_id == user.id)
        )
        for folder in existing_folders.scalars():
            folder_name_to_id[folder.name] = folder.id

    else:
        # Build lookup maps even when not importing feeds (needed for filter scope)
        existing_uf_result = await db.execute(
            select(UserFeed, Feed)
            .join(Feed, Feed.id == UserFeed.feed_id)
            .where(UserFeed.user_id == user.id)
        )
        for uf, feed in existing_uf_result.all():
            feed_url_to_id[feed.feed_url] = uf.feed_id

        existing_folders = await db.execute(
            select(Folder).where(Folder.user_id == user.id)
        )
        for folder in existing_folders.scalars():
            folder_name_to_id[folder.name] = folder.id

    # Pass 3: prefs
    if import_prefs:
        prefs_el = _find_section(body, "tt-rss-prefs")
        if prefs_el is not None:
            settings_result = await db.execute(
                select(UserSettings).where(UserSettings.user_id == user.id)
            )
            us = settings_result.scalar_one_or_none()
            if us is None:
                us = UserSettings(user_id=user.id)
                db.add(us)

            for outline in prefs_el:
                # TTRSS uses pref-name, our export uses text
                key = outline.get("pref-name") or outline.get("text", "")
                value = outline.get("value", "")
                if key == "USER_TIMEZONE" and value:
                    us.timezone = value[:50]
                    result.prefs_updated += 1
                elif key == "PURGE_OLD_DAYS" and value.isdigit():
                    # No global purge setting in our model — skip
                    pass
            await db.commit()

    # Pass 4: filters
    if import_filters:
        filters_el = _find_section(body, "tt-rss-filters")
        if filters_el is not None:
            await _import_filters_element(
                user, filters_el, label_name_to_id, feed_url_to_id, folder_name_to_id, result, db
            )

    return result


def _find_section(body: Element, text: str) -> Element | None:
    for outline in body:
        if outline.get("text") == text:
            return outline
    return None


def _collect_feed_outlines(body: Element) -> list[tuple[Element, str | None]]:
    """Return (outline, folder_name) pairs for all feed outlines in body.

    Handles:
    - flat: <outline xmlUrl="..."/>
    - standard: <outline text="Folder"><outline xmlUrl="..."/></outline>
    - TTRSS: <outline text="All articles"><outline text="Folder"><outline xmlUrl="..."/></outline></outline>
    """
    results: list[tuple[Element, str | None]] = []

    for top in body:
        section_text = top.get("text", "")
        if section_text.startswith("tt-rss-"):
            continue

        if top.get("xmlUrl"):
            # Direct feed at body level
            results.append((top, None))
            continue

        # Could be a folder or a TTRSS wrapper ("All articles")
        # Peek: if children are themselves folder-like (no xmlUrl, have grandchildren with xmlUrl),
        # treat this as a wrapper and unwrap one level
        children = list(top)
        non_section_children = [c for c in children if not c.get("text", "").startswith("tt-rss-")]
        is_wrapper = bool(non_section_children) and all(
            not child.get("xmlUrl") and len(child) > 0
            for child in non_section_children
        )

        if is_wrapper:
            # Unwrap: treat children as folders
            for folder_outline in children:
                folder_name = (folder_outline.get("text") or folder_outline.get("title") or "")[:100]
                if not folder_name or folder_name.startswith("tt-rss-"):
                    continue
                for feed_outline in folder_outline:
                    if feed_outline.get("xmlUrl"):
                        results.append((feed_outline, folder_name))
                    # Ignore deeper nesting beyond 2 levels inside wrapper
        else:
            # Treat as a folder directly
            folder_name = (section_text or top.get("title") or "")[:100]
            for child in children:
                if child.get("xmlUrl"):
                    results.append((child, folder_name or None))

    return results


async def _get_or_create_folder(
    user: User,
    name: str,
    cache: dict[str, int],
    db: AsyncSession,
) -> int:
    if name in cache:
        return cache[name]
    result = await db.execute(
        select(Folder).where(Folder.user_id == user.id, Folder.name == name)
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        folder = Folder(user_id=user.id, name=name)
        db.add(folder)
        await db.flush()
    cache[name] = folder.id
    return folder.id


async def _import_feed(
    user: User,
    outline: Element,
    folder_id: int | None,
    result: ImportResult,
    db: AsyncSession,
) -> int | None:
    """Subscribe user to a single feed outline. Returns new feed_id or None."""
    xml_url = outline.get("xmlUrl", "").strip()
    if not xml_url:
        return None

    title = (outline.get("text") or outline.get("title") or "").strip() or None

    try:
        uf = await subscribe(
            user=user,
            url=xml_url,
            folder_id=folder_id,
            custom_title=title,
            fetch_auth_user=None,
            fetch_auth_pass=None,
            db=db,
        )
        result.feeds_added += 1
        return uf.feed_id
    except ValueError as exc:
        msg = str(exc)
        if "Already subscribed" in msg:
            result.feeds_skipped += 1
        elif "Feed limit" in msg:
            raise _FeedLimitReached()
        else:
            result.feeds_failed += 1
            result.warnings.append(f"Failed to import {xml_url}: {msg}")
        return None
    except Exception as exc:
        result.feeds_failed += 1
        result.warnings.append(f"Failed to import {xml_url}: {exc}")
        return None


async def _import_filters_element(
    user: User,
    filters_el: Element,
    label_name_to_id: dict[str, int],
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
    db: AsyncSession,
) -> None:
    """Import filters from a tt-rss-filters outline element.

    Handles two formats:
    - Our export: element text is a JSON array of filter objects
    - TTRSS export: each child outline has CDATA text = single filter JSON object
    """
    raw_text = (filters_el.text or "").strip()
    children = list(filters_el)

    if raw_text and not children:
        # Our format: JSON array in element text
        try:
            filters_data: list[dict[str, Any]] = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            result.warnings.append(f"Could not parse filters JSON: {exc}")
            return
        if not isinstance(filters_data, list):
            result.warnings.append("Filters data is not a list, skipping")
            return
        await _import_filters(user, filters_data, label_name_to_id, feed_url_to_id, folder_name_to_id, result, db)
    else:
        # TTRSS format: each child outline has CDATA text = single filter JSON object
        filters_data = []
        for child in children:
            raw = (child.text or "").strip()
            if not raw:
                continue
            try:
                fd = json.loads(raw)
                if isinstance(fd, dict):
                    # TTRSS uses "title" key for filter name
                    if "title" in fd and "name" not in fd:
                        fd["name"] = fd["title"]
                    filters_data.append(fd)
            except json.JSONDecodeError as exc:
                result.warnings.append(f"Could not parse filter JSON: {exc}")
        await _import_filters(user, filters_data, label_name_to_id, feed_url_to_id, folder_name_to_id, result, db)


async def _import_filters(
    user: User,
    filters_data: list[dict[str, Any]],
    label_name_to_id: dict[str, int],
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
    db: AsyncSession,
) -> None:
    for i, fd in enumerate(filters_data):
        try:
            name = str(fd.get("name") or f"Imported filter {i + 1}")[:100]

            # Detect format: our own export (has "match_operator") vs TTRSS (has "match_any_rule" / "rules")
            if "match_operator" in fd:
                payload = _parse_filtread_filter(fd, label_name_to_id, feed_url_to_id, folder_name_to_id, result)
            else:
                payload = _parse_ttrss_filter(fd, label_name_to_id, result)

            if payload is None:
                result.filters_skipped += 1
                continue

            payload.name = name
            await create_filter(user.id, payload, db)
            result.filters_added += 1

        except Exception as exc:
            result.filters_skipped += 1
            result.warnings.append(f"Filter '{fd.get('name', i)}' skipped: {exc}")


def _parse_filtread_filter(
    fd: dict,
    label_name_to_id: dict[str, int],
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
) -> FilterCreate | None:
    """Parse our own export format."""
    conditions = []
    for c in fd.get("conditions", []):
        conditions.append(FilterConditionCreate(
            field=c["field"],
            operator=c["operator"],
            value=c["value"],
            position=c.get("position", 0),
        ))

    actions = []
    for a in fd.get("actions", []):
        action_type = a["action_type"]
        action_value = a.get("action_value")
        if action_type == "label" and action_value:
            # action_value may be a label name (from export) or ID string
            if not action_value.isdigit():
                label_id = label_name_to_id.get(action_value)
                if label_id is None:
                    result.warnings.append(f"Label '{action_value}' not found, action skipped")
                    continue
                action_value = str(label_id)
        actions.append(FilterActionCreate(action_type=action_type, action_value=action_value))

    # Resolve scope
    scope_include = _resolve_scope(fd.get("scope_include", []), feed_url_to_id, folder_name_to_id, result)
    scope_except = _resolve_scope(fd.get("scope_except", []), feed_url_to_id, folder_name_to_id, result)

    return FilterCreate(
        name="",
        is_active=bool(fd.get("enabled", True)),
        match_operator=fd.get("match_operator", "AND"),
        stop_on_match=bool(fd.get("stop_on_match", False)),
        scope_include=scope_include,
        scope_except=scope_except,
        conditions=conditions,
        actions=actions,
    )


def _parse_ttrss_filter(
    fd: dict,
    label_name_to_id: dict[str, int],
    result: ImportResult,
) -> FilterCreate | None:
    """Parse TTRSS OPML filter format (best-effort)."""
    match_operator = "OR" if fd.get("match_any_rule") else "AND"

    conditions = []
    for rule in fd.get("rules", []):
        filter_type = rule.get("filter_type")
        our_field = _TTRSS_FIELD_MAP.get(filter_type)
        if our_field is None:
            result.warnings.append(
                f"Filter '{fd.get('name')}': unknown filter_type {filter_type}, rule skipped"
            )
            continue

        value = str(rule.get("reg_exp") or "").strip()
        if not value:
            continue

        inverse = bool(rule.get("inverse"))
        if inverse:
            operator = "not_contains"
        elif _looks_like_regex(value):
            operator = "regex"
        else:
            operator = "contains"

        conditions.append(FilterConditionCreate(field=our_field, operator=operator, value=value))

    if not conditions:
        return None

    actions = []
    for action in fd.get("actions", []):
        action_id = action.get("action_id")
        our_action = _TTRSS_ACTION_MAP.get(action_id)
        if our_action is None:
            continue
        action_value = None
        if our_action == "label":
            param = str(action.get("action_param") or "").strip()
            if not param:
                continue
            label_id = label_name_to_id.get(param)
            if label_id is None:
                result.warnings.append(
                    f"Filter '{fd.get('name')}': label '{param}' not found, action skipped"
                )
                continue
            action_value = str(label_id)
        actions.append(FilterActionCreate(action_type=our_action, action_value=action_value))

    # TTRSS scope is per-ID and doesn't transfer — import as global
    if fd.get("cat_filter") or any(r.get("feed_id") or r.get("cat_id") for r in fd.get("rules", [])):
        result.warnings.append(
            f"Filter '{fd.get('name')}': scope (feed/category) not imported (TTRSS IDs don't transfer)"
        )

    return FilterCreate(
        name="",
        is_active=bool(fd.get("enabled", True)),
        match_operator=match_operator,
        conditions=conditions,
        actions=actions,
    )


def _resolve_scope(
    scope_list: list[str],
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
) -> list[str]:
    resolved = []
    for item in scope_list:
        if item.startswith("feed:"):
            url = item[5:]
            feed_id = feed_url_to_id.get(url)
            if feed_id:
                resolved.append(f"feed:{feed_id}")
            else:
                result.warnings.append(f"Scope feed not found: {url}")
        elif item.startswith("folder:"):
            name = item[7:]
            if name == "__no_folder__":
                resolved.append("folder:0")
            else:
                folder_id = folder_name_to_id.get(name)
                if folder_id:
                    resolved.append(f"folder:{folder_id}")
                else:
                    result.warnings.append(f"Scope folder not found: {name}")
    return resolved

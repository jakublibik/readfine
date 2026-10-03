"""OPML import and export service."""
import asyncio
import json
import logging
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import defusedxml.ElementTree as _safe_ET
from xml.etree.ElementTree import Element, SubElement, indent, register_namespace, tostring

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.feed import Feed, Folder, UserFeed
from app.models.filter import Filter
from app.models.label import Label
from app.models.user import User, UserCatchupConfig, UserSettings
from app.schemas.filter import FilterConditionCreate, FilterActionCreate, FilterCreate
from app.services.ai_profile_service import PROFILE_MAX_CHARS
from app.services.briefing_service import reschedule_briefings
from app.services.feed import AlreadySubscribed, FeedLimitReached, subscribe, subscribe_scrape
from app.services.filter_service import FILTER_ORDER, create_filter
from app.services.folder_service import (
    FOLDER_ORDER_DEFAULT, FOLDER_ORDER_MODES, folder_order_clause, next_folder_position,
)
from app.services.preference_values import (
    DENSITY_VALUES, FONT_FAMILY_VALUES, FONT_SIZE_VALUES, LABEL_DISPLAY_VALUES,
    SORT_VALUES, UNREAD_FILTER_VALUES, clamp_articles_per_page, clamp_buckets,
)
from app.services.relevance_terms_service import TERMS_MAX_CHARS, save_terms
from app.services.saved_search_service import (
    SavedSearchError, create_saved_search, list_saved_searches,
)
from app.services.story_service import DEDUP_VALUES
from app.utils.datetime_format import is_valid_timezone
from app.utils.email_validate import is_valid_email
from app.utils.formats import is_valid_format

logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 1 * 1024 * 1024  # 1 MB

# What a file can carry, in the order the export and import forms list them.
# "feeds" alone is plain OPML. Labels, filters and the timezone use TT-RSS's
# tt-rss-* outlines in <body>, which TT-RSS reads and skips as folders.
SECTIONS = ("feeds", "labels", "filters", "prefs", "profile", "searches", "catchup")

# Everything only Readfine reads lives in <head>, in its own namespace, which is
# how OPML 2.0 allows extensions. Kept out of <body> because a reader that does not
# know a section outline takes it for a folder and creates an empty one.
READFINE_NS = "https://readfine.app/opml"
register_namespace("readfine", READFINE_NS)
_NS_FORMAT = f"{{{READFINE_NS}}}format"
_NS_SECTION = f"{{{READFINE_NS}}}section"

# Written to <head> whenever the file has more than feeds. A file without it is
# plain OPML, TT-RSS, or a Readfine export from before these sections existed.
FORMAT_VERSION = "2"

DEFAULT_LABEL_COLOR = "#6366f1"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SMALLINT_MAX = 32767

# Reading and display preferences with a fixed set of values.
_PREF_CHOICES: dict[str, Collection[str]] = {
    "list_density_web": DENSITY_VALUES,
    "list_density_mobile": DENSITY_VALUES,
    "default_sort_order": SORT_VALUES,
    "unread_filter": UNREAD_FILTER_VALUES,
    "story_dedup": DEDUP_VALUES,
    "label_display": LABEL_DISPLAY_VALUES,
    "reading_font_size": FONT_SIZE_VALUES,
    "reading_font_family": FONT_FAMILY_VALUES,
    "folder_order": FOLDER_ORDER_MODES,
}
_PREF_BOOLS = ("mark_read_on_scroll", "mark_read_auto_advance", "open_original_when_empty")

# File key → settings column for the relevance and AI texts ("profile" section).
# Model and provider choices stay out: they depend on keys, which never leave.
_PROFILE_TEXTS = {
    "relevance_terms": "relevance_terms",
    "ai_profile": "ai_preference_text",
    "ai_summary_prompt": "ai_summary_prompt",
    "ai_context_prompt": "ai_context_prompt",
}

# Feed outline attribute → UserFeed column, for per-feed retention.
_FEED_PURGE_ATTRS = {"purge-after-days": "purge_after_days", "purge-keep-count": "purge_keep_count"}

_CATCHUP_PERIODS = ("today", "yesterday", "7days")
_CATCHUP_STATUSES = ("all", "not_opened")


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
    # TT-RSS "publish/hide" has no Readfine equivalent; map to the closest
    # supported "remove from view" action so the import stays valid.
    "3": "mark_read",
    "4": "star",
    "7": "label",
}

_REGEX_SPECIAL = re.compile(r"[.*+?^${}()|[\]\\]")

_TRUTHY = {"1", "t", "true", "yes", "on"}


def _looks_like_regex(value: str) -> bool:
    return bool(_REGEX_SPECIAL.search(value))


def _truthy(value: Any) -> bool:
    """Normalize a JSON-ish boolean. TTRSS exports raw DB values, so a flag may
    arrive as a real bool, an int (1/0), or a Postgres string ("t"/"f"). Plain
    bool() is wrong here: bool("f") is True."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY
    return bool(value)


# ── Export ────────────────────────────────────────────────────────────────────

async def export_opml(user: User, db: AsyncSession, sections: Collection[str] | None = None) -> str:
    """Build and return an OPML 2.0 XML string for the user's subscriptions.

    ``sections`` picks what goes in (see SECTIONS); None means everything. With
    only "feeds" the file is plain OPML that any reader takes as is.
    """
    include = set(SECTIONS) if sections is None else set(sections) & set(SECTIONS)

    # Load data
    settings_result = await db.execute(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    user_settings = settings_result.scalar_one_or_none()

    # Folder outlines come out in the order the user has them in, not always
    # alphabetically: the file is a picture of their subscriptions, and a reader
    # importing it has nothing else to go on for how to order them.
    folders_result = await db.execute(
        select(Folder).where(Folder.user_id == user.id).order_by(
            *folder_order_clause(
                user_settings.folder_order if user_settings else FOLDER_ORDER_DEFAULT
            )
        )
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
        select(Label).where(Label.user_id == user.id).order_by(Label.position, func.lower(Label.name))
    )
    labels = labels_result.scalars().all()

    # Filters, saved searches and catch-ups refer to feeds, folders and labels by
    # id; the file names them by URL and name instead, which is what survives a
    # move to another account or instance.
    refs = _ExportRefs(
        feed_urls={feed.id: feed.feed_url for _, feed in user_feeds},
        folder_names={f.id: f.name for f in folders.values()},
        label_names={label.id: label.name for label in labels},
    )

    # Build XML
    root = Element("opml", version="2.0")
    head = SubElement(root, "head")
    SubElement(head, "title").text = "Readfine subscriptions"
    SubElement(head, "dateCreated").text = datetime.now(timezone.utc).strftime(
        "%a, %d %b %Y %H:%M:%S +0000"
    )
    if include - {"feeds"}:
        SubElement(head, _NS_FORMAT).text = FORMAT_VERSION

    body = SubElement(root, "body")

    if "feeds" in include:
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

    # Labels section. Listed in the order the user keeps them; the import appends
    # them in file order, which is what carries that order over.
    if "labels" in include and labels:
        labels_el = SubElement(body, "outline", text="tt-rss-labels")
        for label in labels:
            SubElement(
                labels_el,
                "outline",
                text=f"-{label.name}",
                **{"label-name": label.name, "label-bg-color": label.color},
            )

    if "prefs" in include and user_settings:
        # The timezone stays where TT-RSS keeps it, so a TT-RSS import still finds it.
        if user_settings.timezone:
            prefs_el = SubElement(body, "outline", text="tt-rss-prefs")
            SubElement(prefs_el, "outline", text="USER_TIMEZONE", value=user_settings.timezone)
        _head_section(head, "prefs", _export_prefs(user_settings))

    if "profile" in include and user_settings:
        profile = _export_profile(user_settings)
        if profile:
            _head_section(head, "profile", profile)

    if "filters" in include:
        filters_result = await db.execute(
            select(Filter)
            .where(Filter.user_id == user.id)
            .options(selectinload(Filter.conditions), selectinload(Filter.actions))
            .order_by(*FILTER_ORDER)
        )
        filters_payload = [_export_filter(f, refs) for f in filters_result.scalars().all()]
        if filters_payload:
            SubElement(body, "outline", text="tt-rss-filters").text = json.dumps(
                filters_payload, ensure_ascii=False
            )

    if "searches" in include:
        searches = await list_saved_searches(db, user.id)
        if searches:
            _head_section(head, "saved-searches", [
                {"name": s.name, "params": _export_search_params(s.params, refs)}
                for s in searches
            ])

    if "catchup" in include:
        configs = (await db.execute(
            select(UserCatchupConfig)
            .where(UserCatchupConfig.user_id == user.id)
            .order_by(UserCatchupConfig.name, UserCatchupConfig.id)
        )).scalars().all()
        if configs:
            _head_section(head, "catchup", [_export_catchup(c, refs) for c in configs])

    indent(root, space="  ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode")


@dataclass
class _ExportRefs:
    feed_urls: dict[int, str]
    folder_names: dict[int, str]
    label_names: dict[int, str]


def _head_section(head: Element, name: str, payload: Any) -> None:
    SubElement(head, _NS_SECTION, name=name).text = json.dumps(payload, ensure_ascii=False)


def _feed_outline(parent: Element, uf: UserFeed, feed: Feed) -> None:
    title = uf.custom_title or feed.title or feed.feed_url
    attrs: dict[str, str] = {
        "text": title,
        "title": title,
        # Kept as "rss" so other OPML readers still treat the outline as a feed;
        # Readfine's own import keys on the "feed-type" attribute below, not "type".
        "type": "rss",
        "xmlUrl": feed.feed_url,
    }
    if feed.site_url:
        attrs["htmlUrl"] = feed.site_url
    # Scrape feeds carry their CSS selector in custom attributes so a Readfine
    # export round-trips back into Readfine (OPML has no native scrape support).
    if feed.feed_type == "scrape":
        selector = (feed.type_config or {}).get("article_links_selector")
        if selector:
            attrs["feed-type"] = "scrape"
            attrs["article-links-selector"] = selector
    # The subscription's own settings, in attributes other readers ignore.
    extract = getattr(uf, "extract_readable", None)
    if extract is not None:
        attrs["extract-readable"] = "1" if extract else "0"
    ai_summary = getattr(uf, "ai_summary_enabled", None)
    if ai_summary is not None:
        attrs["ai-summary"] = "1" if ai_summary else "0"
    for attr, column in _FEED_PURGE_ATTRS.items():
        value = getattr(uf, column, None)
        if value is not None:
            attrs[attr] = str(value)
    SubElement(parent, "outline", **attrs)


def _export_filter(f: Filter, refs: _ExportRefs) -> dict:
    actions = []
    for a in f.actions:
        value = a.action_value
        if a.action_type == "label":
            # By name: an id means nothing in another account. A label that no
            # longer exists leaves nothing to name, so the action is left out.
            value = refs.label_names.get(_int_or_none(value))
            if value is None:
                continue
        actions.append({"action_type": a.action_type, "action_value": value})
    return {
        "name": f.name,
        "enabled": f.is_active,
        "position": f.position,
        "match_operator": f.match_operator,
        "stop_on_match": f.stop_on_match,
        "scope_include": _scope_to_urls(f.scope_include, refs.feed_urls, refs.folder_names),
        "scope_except": _scope_to_urls(f.scope_except, refs.feed_urls, refs.folder_names),
        "conditions": [
            {
                "field": c.field,
                "operator": c.operator,
                "value": c.value,
                "position": c.position,
            }
            for c in sorted(f.conditions, key=lambda x: x.position)
        ],
        "actions": actions,
    }


def _export_prefs(s: UserSettings) -> dict:
    prefs: dict[str, Any] = {key: getattr(s, key) for key in _PREF_CHOICES}
    prefs.update({key: getattr(s, key) for key in _PREF_BOOLS})
    for key in ("articles_per_page", "bucket_small_max", "bucket_medium_max", "format_profile"):
        prefs[key] = getattr(s, key)
    return {k: v for k, v in prefs.items() if v is not None}


def _export_profile(s: UserSettings) -> dict:
    profile: dict[str, Any] = {"basic_scoring_enabled": s.basic_scoring_enabled}
    for key, column in _PROFILE_TEXTS.items():
        value = getattr(s, column)
        if value:
            profile[key] = value
    return profile


def _export_search_params(params: dict, refs: _ExportRefs) -> dict:
    out = dict(params)
    if out.get("scope_include"):
        out["scope_include"] = _scope_to_urls(out["scope_include"], refs.feed_urls, refs.folder_names)
    if out.get("label_filter"):
        out["label_filter"] = _labels_to_names(out["label_filter"], refs.label_names)
    return out


def _export_catchup(c: UserCatchupConfig, refs: _ExportRefs) -> dict:
    item: dict[str, Any] = {
        "name": c.name,
        "period": c.period,
        "filter_status": c.filter_status,
        "scope_include": _scope_to_urls(c.scope_include, refs.feed_urls, refs.folder_names),
        "label_filter": _labels_to_names(c.label_filter, refs.label_names),
        # Stored as a fraction, shown and written as a score out of 100.
        "score_min": round(c.filter_score_min * 100) if c.filter_score_min is not None else None,
        "article_limit": c.article_limit,
        "custom_prompt": c.custom_prompt,
        "include_snippet": c.include_snippet,
    }
    if c.briefing_interval and c.briefing_time:
        item["briefing"] = {
            "enabled": c.briefing_enabled,
            "interval": c.briefing_interval,
            "day": c.briefing_day,
            "time": c.briefing_time,
            "recipients": _json_list(c.briefing_recipients),
        }
    return item


def _json_list(value: str | list | None) -> list:
    """A column holding a JSON array (or already a list) as a list; junk is empty."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        items = json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []
    return items if isinstance(items, list) else []


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):  # JSON allows Infinity
        return None


def _labels_to_names(tokens: str | list | None, label_names: dict[int, str]) -> list[str]:
    """``["any"]`` stays; ``label:<id>`` becomes ``label:<name>``, dropping deleted labels."""
    items = _json_list(tokens)
    if "any" in items:
        return ["any"]
    out = []
    for item in items:
        if isinstance(item, str) and item.startswith("label:"):
            name = label_names.get(_int_or_none(item[6:]))
            if name is not None:
                out.append(f"label:{name}")
    return out


def _scope_to_urls(
    scope: str | list | None,
    feed_id_to_url: dict[int, str],
    folder_id_to_name: dict[int, str],
) -> list[str]:
    result = []
    for item in _json_list(scope):
        if not isinstance(item, str):
            continue
        if item.startswith("feed:"):
            url = feed_id_to_url.get(_int_or_none(item[5:]))
            if url:
                result.append(f"feed:{url}")
        elif item.startswith("folder:"):
            folder_id_str = item[7:]
            if folder_id_str == "0":
                result.append("folder:__no_folder__")
            else:
                name = folder_id_to_name.get(_int_or_none(folder_id_str))
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
    profile_updated: int = 0
    filters_added: int = 0
    filters_skipped: int = 0
    searches_added: int = 0
    searches_skipped: int = 0
    catchups_added: int = 0
    catchups_skipped: int = 0
    warnings: list[str] = field(default_factory=list)
    # Set when the subscription cap stopped the import part way. feeds_over_limit is
    # how many outlines were never even attempted, so it counts duplicates the import
    # would have skipped anyway — it is "left out", not "would have been added". Kept
    # as fields rather than a warning line because the number is the whole point: a
    # migration cut from 180 feeds to 50 has to say so where it cannot be missed.
    feeds_over_limit: int = 0
    feed_limit: int | None = None


class _LabelBook:
    """The user's labels by name, creating the ones the file refers to but the account lacks.

    A filter, saved search or catch-up that labels or selects by a missing label
    gets that label rather than losing the reference. Its colour comes from the
    file's label section when the file has one. New labels go after the existing
    ones, in the order they are met, so a file's label order carries over.
    """

    def __init__(self, user_id: int, result: ImportResult):
        self.user_id = user_id
        self.result = result
        self.ids: dict[str, int] = {}
        self.colors: dict[str, str] = {}
        self.file_order: list[str] = []
        self._next_position = 0

    async def load(self, db: AsyncSession, section: Element | None) -> None:
        for label in (await db.execute(select(Label).where(Label.user_id == self.user_id))).scalars():
            self.ids[label.name] = label.id
            self._next_position = max(self._next_position, label.position + 1)
        for outline in section if section is not None else []:
            name = (outline.get("label-name") or outline.get("text", "").lstrip("-")).strip()[:100]
            if not name or name in self.colors:
                continue
            color = (outline.get("label-bg-color") or outline.get("label-fg-color") or "").strip()
            self.colors[name] = color if _COLOR_RE.match(color) else DEFAULT_LABEL_COLOR
            self.file_order.append(name)

    async def ensure(self, name: str, db: AsyncSession) -> int | None:
        name = name.strip()[:100]
        if not name:
            return None
        if name in self.ids:
            return self.ids[name]
        label = Label(
            user_id=self.user_id, name=name,
            color=self.colors.get(name, DEFAULT_LABEL_COLOR), position=self._next_position,
        )
        self._next_position += 1
        db.add(label)
        await db.flush()
        self.ids[name] = label.id
        self.result.labels_added += 1
        return label.id

    async def import_section(self, db: AsyncSession) -> None:
        for name in self.file_order:
            if name in self.ids:
                self.result.labels_skipped += 1
            else:
                await self.ensure(name, db)

    async def tokens(self, items: Any, db: AsyncSession) -> list[str]:
        """File label tokens (``any`` / ``label:<name>``) back to ``label:<id>``."""
        items = _json_list(items)
        if "any" in items:
            return ["any"]
        out = []
        for item in items:
            if isinstance(item, str) and item.startswith("label:"):
                label_id = await self.ensure(item[6:], db)
                if label_id is not None:
                    out.append(f"label:{label_id}")
        return out


async def import_opml(
    user: User,
    xml_bytes: bytes,
    import_feeds: bool,
    import_labels: bool,
    import_prefs: bool,
    import_filters: bool,
    db: AsyncSession,
    import_profile: bool = False,
    import_searches: bool = False,
    import_catchup: bool = False,
) -> ImportResult:
    """Import subscriptions, labels, settings, filters, saved searches and catch-ups.

    Not atomic: each pass commits independently, so a late failure can leave earlier
    passes persisted. This is intentional — the import is idempotent: existing
    labels/feeds/folders/filters/searches/catch-ups are detected and skipped, so
    re-running after a failure converges without creating duplicates. Settings are
    only ever set, never cleared: a value the file lacks leaves the account's alone.
    """
    result = ImportResult()

    try:
        root = _safe_ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))
    except _safe_ET.ParseError as exc:
        raise ValueError(f"Invalid OPML file: {exc}") from exc

    body = root.find("body")
    if body is None:
        raise ValueError("OPML file has no <body> element")
    versioned = root.find(f"head/{_NS_FORMAT}") is not None

    # Pass 1: labels first, the later passes refer to them by name.
    labels = _LabelBook(user.id, result)
    if import_labels or import_filters or import_searches or import_catchup:
        await labels.load(db, _find_section(body, "tt-rss-labels"))
        if import_labels:
            await labels.import_section(db)
        await db.commit()

    # Pass 2: import feeds + folders
    new_feed_ids: list[int] = []  # collected for deferred initial fetch
    feed_url_to_id: dict[str, int] = {}
    if import_feeds:
        # Collect all top-level feed outlines, unwrapping TTRSS "All articles" wrapper
        feed_outlines = _collect_feed_outlines(body)
        folder_cache: dict[str, int] = {}
        next_folder_pos = await next_folder_position(db, user.id)
        for index, (outline, folder_name) in enumerate(feed_outlines):
            folder_id = None
            if folder_name:
                folder_id, next_folder_pos = await _get_or_create_folder(
                    user, folder_name, folder_cache, db, next_folder_pos
                )
            xml_url = outline.get("xmlUrl", "")
            try:
                added_id = await _import_feed(user, outline, folder_id, result, db)
            except FeedLimitReached as exc:
                # The one that raised was not imported either, so it counts as left out.
                result.feeds_over_limit = len(feed_outlines) - index
                result.feed_limit = exc.max_feeds
                break
            if added_id and xml_url:
                feed_url_to_id[xml_url] = added_id
                # Scrape feeds already trigger their own background fetch in
                # subscribe_scrape; don't queue them for the RSS _initial_fetch.
                if (outline.get("feed-type") or "").strip().lower() != "scrape":
                    new_feed_ids.append(added_id)

    # Lookup maps for scope resolution, from the subscriptions as they are now.
    # The URL a feed was just added under wins over the one it is stored as, since
    # the file's scopes use the former.
    feed_title_to_id: dict[str, int] = {}  # TTRSS filters scope feeds by title, not URL
    folder_name_to_id: dict[str, int] = {}
    if import_filters or import_searches or import_catchup:
        existing_uf_result = await db.execute(
            select(UserFeed, Feed)
            .join(Feed, Feed.id == UserFeed.feed_id)
            .where(UserFeed.user_id == user.id)
        )
        for uf, feed in existing_uf_result.all():
            feed_url_to_id.setdefault(feed.feed_url, uf.feed_id)
            title = (uf.custom_title or feed.title or "").strip()
            if title:
                feed_title_to_id.setdefault(title, uf.feed_id)

        existing_folders = await db.execute(
            select(Folder).where(Folder.user_id == user.id)
        )
        for folder in existing_folders.scalars():
            folder_name_to_id[folder.name] = folder.id

    # Pass 3: settings
    if import_prefs or import_profile:
        us = await db.scalar(select(UserSettings).where(UserSettings.user_id == user.id))
        if us is None:
            us = UserSettings(user_id=user.id)
            db.add(us)
        if import_prefs:
            await _import_prefs(user, root, us, result, db)
        if import_profile:
            data = _readfine_section(root, "profile", dict, result)
            if data:
                _apply_profile(us, data, result)
        await db.commit()

    # Pass 4: filters
    if import_filters:
        filters_el = _find_section(body, "tt-rss-filters")
        if filters_el is not None:
            await _import_filters_element(
                user, filters_el, labels, feed_url_to_id, feed_title_to_id,
                folder_name_to_id, result, db, versioned=versioned,
            )

    # Pass 5: saved searches and catch-ups
    if import_searches:
        data = _readfine_section(root, "saved-searches", list, result)
        if data:
            await _import_saved_searches(
                user, data, labels, feed_url_to_id, folder_name_to_id, result, db
            )
    if import_catchup:
        data = _readfine_section(root, "catchup", list, result)
        if data:
            await _import_catchups(
                user, data, labels, feed_url_to_id, folder_name_to_id, result, db
            )

    # Kick off initial fetches after all filters are in DB. Mark in-progress
    # synchronously before spawning (the task no longer self-marks — see subscribe()).
    import app.database as db_module
    from app.services.feed import _initial_fetch, _initial_fetch_in_progress
    if new_feed_ids and db_module.async_session_factory is not None:
        for feed_id in new_feed_ids:
            if feed_id in _initial_fetch_in_progress:
                continue
            _initial_fetch_in_progress.add(feed_id)
            asyncio.create_task(_initial_fetch(feed_id))

    return result


def _is_section(text: str) -> bool:
    """The outlines that carry settings rather than feeds."""
    return text.startswith("tt-rss-")


def _readfine_section(root: Element, name: str, kind: type, result: ImportResult) -> Any:
    """The JSON payload of one of our <head> sections, or None."""
    for el in root.findall(f"head/{_NS_SECTION}"):
        if el.get("name") == name:
            return _section_json(el, kind, name.replace("-", " "), result)
    return None


def _find_section(body: Element, text: str) -> Element | None:
    for outline in body:
        if outline.get("text") == text:
            return outline
    return None


def _section_json(section: Element, kind: type, what: str, result: ImportResult) -> Any:
    """A section's JSON payload if it is the expected kind, else None with a warning."""
    raw = (section.text or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        result.warnings.append(f"Could not parse {what}: {exc}")
        return None
    if not isinstance(data, kind):
        result.warnings.append(f"Could not parse {what}: unexpected format, skipped")
        return None
    return data


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
        if _is_section(section_text):
            continue

        if top.get("xmlUrl"):
            # Direct feed at body level
            results.append((top, None))
            continue

        # Could be a folder or a TTRSS wrapper ("All articles")
        # Peek: if children are themselves folder-like (no xmlUrl, have grandchildren with xmlUrl),
        # treat this as a wrapper and unwrap one level
        children = list(top)
        non_section_children = [c for c in children if not _is_section(c.get("text", ""))]
        is_wrapper = bool(non_section_children) and all(
            not child.get("xmlUrl") and len(child) > 0
            for child in non_section_children
        )

        if is_wrapper:
            # Unwrap: treat children as folders
            for folder_outline in children:
                folder_name = (folder_outline.get("text") or folder_outline.get("title") or "")[:100]
                if not folder_name or _is_section(folder_name):
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
    next_position: int,
) -> tuple[int, int]:
    """Return the folder's id and the position the next new folder should take.

    The caller counts positions up instead of asking the database per folder:
    an import creating thirty folders would otherwise run thirty MAX() queries,
    each one relying on the previous folder having been flushed already.
    """
    if name in cache:
        return cache[name], next_position
    result = await db.execute(
        select(Folder).where(Folder.user_id == user.id, Folder.name == name)
    )
    folder = result.scalar_one_or_none()
    if folder is None:
        folder = Folder(user_id=user.id, name=name, position=next_position)
        next_position += 1
        db.add(folder)
        await db.flush()
    cache[name] = folder.id
    return folder.id, next_position


def _feed_options(outline: Element) -> dict[str, Any]:
    """The subscription settings a Readfine export writes on a feed outline."""
    options: dict[str, Any] = {}
    for attr, column in (("extract-readable", "extract_readable"), ("ai-summary", "ai_summary_enabled")):
        value = outline.get(attr)
        if value is not None:
            options[column] = _truthy(value)
    for attr, column in _FEED_PURGE_ATTRS.items():
        value = _int_or_none(outline.get(attr))
        if value is not None and 1 <= value <= _SMALLINT_MAX:
            options[column] = value
    return options


async def _import_feed(
    user: User,
    outline: Element,
    folder_id: int | None,
    result: ImportResult,
    db: AsyncSession,
) -> int | None:
    """Subscribe user to a single feed outline. Returns new feed_id or None.

    The outline's subscription settings apply only to a feed the import adds: one
    the account already has keeps whatever it was set to here.
    """
    xml_url = outline.get("xmlUrl", "").strip()
    if not xml_url:
        return None

    title = (outline.get("text") or outline.get("title") or "").strip() or None
    feed_type = (outline.get("feed-type") or "").strip().lower()

    try:
        if feed_type == "scrape":
            selector = (outline.get("article-links-selector") or "").strip()
            if not selector:
                result.feeds_failed += 1
                result.warnings.append(
                    f"Failed to import {xml_url}: scrape feed is missing its CSS selector"
                )
                return None
            # Trust the backup: skip live selector validation (the page may be
            # temporarily down or changed). subscribe_scrape still kicks off the
            # first scrape on a background task, like a normal scrape subscribe.
            uf = await subscribe_scrape(
                user=user,
                url=xml_url,
                selector=selector,
                title=title or xml_url,
                folder_id=folder_id,
                db=db,
                validate_selector=False,
            )
        else:
            uf = await subscribe(
                user=user,
                url=xml_url,
                folder_id=folder_id,
                custom_title=title,
                fetch_auth_user=None,
                fetch_auth_pass=None,
                db=db,
                trigger_initial_fetch=False,
            )
    except AlreadySubscribed:
        result.feeds_skipped += 1
        return None
    except FeedLimitReached:
        raise  # propagate to the import loop, which stops and warns
    except Exception as exc:
        result.feeds_failed += 1
        result.warnings.append(f"Failed to import {xml_url}: {exc}")
        return None

    result.feeds_added += 1
    options = _feed_options(outline)
    if options:
        for column, value in options.items():
            setattr(uf, column, value)
        await db.commit()
    return uf.feed_id


async def _import_filters_element(
    user: User,
    filters_el: Element,
    labels: _LabelBook,
    feed_url_to_id: dict[str, int],
    feed_title_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
    db: AsyncSession,
    versioned: bool = False,
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
        filters_data = _section_json(filters_el, list, "filters", result)
        if filters_data is None:
            return
        await _import_filters(user, filters_data, labels, feed_url_to_id, feed_title_to_id, folder_name_to_id, result, db, versioned)
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
        await _import_filters(user, filters_data, labels, feed_url_to_id, feed_title_to_id, folder_name_to_id, result, db)


def _dedupe_conditions(
    conditions: list[FilterConditionCreate],
) -> list[FilterConditionCreate]:
    """Drop duplicate conditions, keeping the first occurrence (and its position)."""
    seen: set[tuple[str, str, str]] = set()
    deduped: list[FilterConditionCreate] = []
    for c in conditions:
        key = (c.field, c.operator, c.value)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped


def _filter_fingerprint(name: str, conditions: list[FilterConditionCreate]) -> tuple:
    """Return a hashable key representing (name, conditions) for duplicate detection."""
    cond_key = frozenset((c.field, c.operator, c.value) for c in conditions)
    return (name, cond_key)


async def _import_filters(
    user: User,
    filters_data: list[dict[str, Any]],
    labels: _LabelBook,
    feed_url_to_id: dict[str, int],
    feed_title_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
    db: AsyncSession,
    versioned: bool = False,
) -> None:
    # Build set of existing filter fingerprints for duplicate detection
    existing_result = await db.execute(
        select(Filter)
        .where(Filter.user_id == user.id)
        .options(selectinload(Filter.conditions))
    )
    existing_fingerprints: set[tuple] = set()
    for f in existing_result.scalars():
        cond_key = frozenset((c.field, c.operator, c.value) for c in f.conditions)
        existing_fingerprints.add((f.name, cond_key))

    for i, fd in enumerate(filters_data):
        if not isinstance(fd, dict):
            continue
        try:
            name = str(fd.get("name") or f"Imported filter {i + 1}")[:100]

            # Detect format: our own export (has "match_operator") vs TTRSS (has "match_any_rule" / "rules")
            is_readfine = "match_operator" in fd

            # A label the filter applies but the account lacks is created, so the
            # action survives the move instead of being dropped. Except for digits
            # in a Readfine file from before the format marker: those exports wrote
            # the id of a label deleted since (deleting one leaves its filter
            # actions in place) and creating a label called "17" helps nobody.
            for label_name in _filter_label_names(fd, is_readfine):
                if is_readfine and not versioned and label_name.isdigit()                         and label_name not in labels.ids:
                    continue
                await labels.ensure(label_name, db)
            await db.commit()

            if is_readfine:
                payload = _parse_readfine_filter(fd, labels.ids, feed_url_to_id, folder_name_to_id, result)
            else:
                payload = _parse_ttrss_filter(
                    fd, labels.ids, feed_title_to_id, folder_name_to_id, result
                )

            if payload is None:
                result.filters_skipped += 1
                continue

            payload.name = name
            # Collapse duplicate conditions. TTRSS scope is per-rule, so once we
            # factor it up to the filter (e.g. a "match-all on 9 feeds" filter),
            # the rules degenerate into identical (field, operator, value) triples;
            # in Readfine a repeated condition is a pure no-op either way.
            payload.conditions = _dedupe_conditions(payload.conditions)

            fp = _filter_fingerprint(name, payload.conditions)
            if fp in existing_fingerprints:
                result.filters_skipped += 1
                continue

            await create_filter(user.id, payload, db)
            existing_fingerprints.add(fp)
            result.filters_added += 1

        except Exception as exc:
            result.filters_skipped += 1
            result.warnings.append(f"Filter '{fd.get('name', i)}' skipped: {exc}")


def _filter_label_names(fd: dict, is_readfine: bool) -> list[str]:
    """Names of the labels a filter's actions apply, in either format."""
    names = []
    for action in fd.get("actions") or []:
        if not isinstance(action, dict):
            continue
        if is_readfine:
            if action.get("action_type") == "label":
                names.append(str(action.get("action_value") or ""))
        elif _TTRSS_ACTION_MAP.get(str(action.get("action_id"))) == "label":
            names.append(str(action.get("action_param") or ""))
    return [n.strip() for n in names if n.strip()]


def _parse_readfine_filter(
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
        if action_type == "label":
            # Always a label name. Files written before names were used could hold
            # an id, but an id from another account never points anywhere useful,
            # and treating digits as one broke labels named like "2024".
            name = str(action_value or "").strip()
            label_id = label_name_to_id.get(name)
            if label_id is None:
                result.warnings.append(f"Label '{name}' not found, action skipped")
                continue
            action_value = str(label_id)
        actions.append(FilterActionCreate(action_type=action_type, action_value=action_value))

    # Resolve scope
    wanted_include = fd.get("scope_include") or []
    scope_include = _resolve_scope(wanted_include, feed_url_to_id, folder_name_to_id, result)
    scope_except = _resolve_scope(fd.get("scope_except") or [], feed_url_to_id, folder_name_to_id, result)

    is_active = _truthy(fd.get("enabled", True))
    if wanted_include and not scope_include and is_active:
        # None of the feeds or folders it was limited to are here, and an empty
        # scope means every feed: a "mark read" meant for one feed would hit all
        # of them. Imported switched off, for the user to rescope.
        is_active = False
        result.warnings.append(
            f"Filter '{fd.get('name')}': none of its feeds or folders were found, "
            f"imported switched off (set its scope and switch it on)"
        )

    position = _int_or_none(fd.get("position"))
    return FilterCreate(
        name="",
        is_active=is_active,
        match_operator=fd.get("match_operator", "AND"),
        position=position if position is not None and 0 <= position <= _SMALLINT_MAX else 0,
        stop_on_match=_truthy(fd.get("stop_on_match", False)),
        scope_include=scope_include,
        scope_except=scope_except,
        conditions=conditions,
        actions=actions,
    )


def _resolve_named_scope(
    name: str,
    is_cat: bool,
    feed_title_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    unresolved: set[str],
) -> str | None:
    """Resolve a TTRSS scope target (feed title or category name) to a Readfine
    scope token. TTRSS categories map to Readfine folders. Records misses in
    `unresolved` so the caller can warn."""
    if is_cat:
        folder_id = folder_name_to_id.get(name)
        if folder_id:
            return f"folder:{folder_id}"
    else:
        feed_id = feed_title_to_id.get(name)
        if feed_id:
            return f"feed:{feed_id}"
    unresolved.add(name)
    return None


def _rule_scope_targets(
    rule: dict,
    feed_title_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    unresolved: set[str],
) -> tuple[list[str], bool]:
    """Return (scope_tokens, is_global) for a single TTRSS rule.

    is_global means the rule applies to all feeds (no scope). Two on-disk shapes:
    - newer "match" array: [[name, is_cat, is_zero], ...] — is_zero (feed id 0) = all
    - classic single target: "feed" title string + "cat_filter" bool ("" = all)
    Both reference feeds/categories by name (TTRSS resolves ids to titles on export),
    so a miss means the feed/folder isn't subscribed, not that the format is unknown.
    """
    match = rule.get("match")
    if isinstance(match, list) and match:
        tokens: list[str] = []
        for entry in match:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            name, is_cat = entry[0], _truthy(entry[1])
            is_zero = _truthy(entry[2]) if len(entry) > 2 else False
            if is_zero or not name or name == 0:
                return [], True  # an "all feeds/categories" entry makes the rule global
            tok = _resolve_named_scope(str(name), is_cat, feed_title_to_id, folder_name_to_id, unresolved)
            if tok:
                tokens.append(tok)
        return tokens, False

    feed_name = str(rule.get("feed") or "").strip()
    if not feed_name:
        return [], True
    tok = _resolve_named_scope(
        feed_name, _truthy(rule.get("cat_filter")), feed_title_to_id, folder_name_to_id, unresolved
    )
    return ([tok] if tok else []), False


def _parse_ttrss_filter(
    fd: dict,
    label_name_to_id: dict[str, int],
    feed_title_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
) -> FilterCreate | None:
    """Parse TTRSS OPML filter format (best-effort)."""
    match_operator = "OR" if _truthy(fd.get("match_any_rule")) else "AND"

    conditions = []
    rule_scopes: list[tuple[list[str], bool]] = []  # (tokens, is_global) per kept rule
    unresolved_scope: set[str] = set()
    for rule in fd.get("rules", []):
        # TTRSS exports raw DB values without casting, so filter_type may be an
        # int (1) or a string ("1"); our map is keyed by string.
        filter_type = rule.get("filter_type")
        our_field = _TTRSS_FIELD_MAP.get(str(filter_type)) if filter_type is not None else None
        if our_field is None:
            result.warnings.append(
                f"Filter '{fd.get('name')}': unknown filter_type {filter_type}, rule skipped"
            )
            continue

        value = str(rule.get("reg_exp") or "").strip()
        if not value:
            continue

        inverse = _truthy(rule.get("inverse"))
        if inverse:
            operator = "not_contains"
        elif _looks_like_regex(value):
            operator = "regex"
        else:
            operator = "contains"

        conditions.append(FilterConditionCreate(field=our_field, operator=operator, value=value))
        # Track scope only for rules that contribute a condition; a dropped rule's
        # scope is moot.
        rule_scopes.append(
            _rule_scope_targets(rule, feed_title_to_id, folder_name_to_id, unresolved_scope)
        )

    if not conditions:
        return None

    actions = []
    for action in fd.get("actions", []):
        action_id = action.get("action_id")
        our_action = _TTRSS_ACTION_MAP.get(str(action_id)) if action_id is not None else None
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

    # TTRSS scope is per-rule; Readfine scope is per-filter. We can only safely
    # factor it out when *every* kept rule is feed/category-scoped (none global).
    # If the filter mixes scoped and global rules, applying scope would wrongly
    # narrow the global rules, so we import it as global with a warning.
    scope_include = _derive_filter_scope(fd.get("name"), rule_scopes, unresolved_scope, result)

    return FilterCreate(
        name="",
        is_active=_truthy(fd.get("enabled", True)),
        match_operator=match_operator,
        scope_include=scope_include,
        conditions=conditions,
        actions=actions,
    )


def _derive_filter_scope(
    filter_name: Any,
    rule_scopes: list[tuple[list[str], bool]],
    unresolved_scope: set[str],
    result: ImportResult,
) -> list[str]:
    """Collapse per-rule TTRSS scope into a single per-filter scope_include.

    Returns [] (global) unless every rule is scoped and at least one target
    resolved. See _parse_ttrss_filter for the safety rationale.
    """
    scoped = [tokens for tokens, is_global in rule_scopes if not is_global]
    has_global = any(is_global for _, is_global in rule_scopes)

    if not scoped:
        return []  # no rule carried scope — a plain global filter, nothing to warn about

    if has_global:
        result.warnings.append(
            f"Filter '{filter_name}': scope not applied — mixes feed-scoped and global "
            f"rules, imported as global (review scope manually)"
        )
        return []

    # Every rule is scoped. Union the resolved targets (exact for shared scope, a
    # minor broadening for OR filters with differing per-rule scope — acceptable).
    tokens = sorted({tok for group in scoped for tok in group})

    if unresolved_scope:
        result.warnings.append(
            f"Filter '{filter_name}': scope targets not found, skipped: "
            f"{', '.join(sorted(unresolved_scope))}"
        )

    if not tokens:
        result.warnings.append(
            f"Filter '{filter_name}': scope could not be resolved, imported as global"
        )
        return []

    return tokens


def _resolve_scope(
    scope_list: list[str],
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
) -> list[str]:
    resolved = []
    for item in scope_list:
        if not isinstance(item, str):
            continue
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


# ── Settings, saved searches, catch-ups ───────────────────────────────────────

async def _import_prefs(
    user: User, root: Element, us: UserSettings, result: ImportResult, db: AsyncSession,
) -> None:
    prefs_el = _find_section(root.find("body"), "tt-rss-prefs")
    for outline in prefs_el if prefs_el is not None else []:
        # TTRSS uses pref-name, our export uses text
        key = outline.get("pref-name") or outline.get("text", "")
        value = outline.get("value", "")
        if key == "USER_TIMEZONE" and value:
            # TTRSS allows "Automatic" and other non-IANA values; only accept
            # names zoneinfo can resolve, otherwise downstream tz math breaks.
            if not is_valid_timezone(value):
                result.warnings.append(f"Ignored unsupported timezone '{value}'")
            elif value != us.timezone:
                us.timezone = value[:50]
                result.prefs_updated += 1
                # Briefings are sent at a local time, so they move with the zone.
                await reschedule_briefings(user.id, us.timezone, db)
        # PURGE_OLD_DAYS and the rest of TT-RSS's prefs have no per-user
        # equivalent here and are skipped.

    data = _readfine_section(root, "prefs", dict, result)
    if data:
        _apply_prefs(us, data, result)


def _apply_prefs(us: UserSettings, data: dict, result: ImportResult) -> None:
    """Set the reading and display preferences the file holds, each only if valid."""
    changed: dict[str, Any] = {}
    for key, allowed in _PREF_CHOICES.items():
        if key in data:
            # A list or object from a damaged file is not hashable: check the type
            # before asking a set whether it holds the value.
            if isinstance(data[key], str) and data[key] in allowed:
                changed[key] = data[key]
            else:
                result.warnings.append(f"Ignored preference {key} = {data[key]!r}")
    for key in _PREF_BOOLS:
        if isinstance(data.get(key), bool):
            changed[key] = data[key]
    if isinstance(data.get("format_profile"), str) and is_valid_format(data["format_profile"]):
        changed["format_profile"] = data["format_profile"]
    per_page = _int_or_none(data.get("articles_per_page"))
    if per_page is not None:
        changed["articles_per_page"] = clamp_articles_per_page(per_page)
    small, medium = _int_or_none(data.get("bucket_small_max")), _int_or_none(data.get("bucket_medium_max"))
    if small is not None and medium is not None:
        changed["bucket_small_max"], changed["bucket_medium_max"] = clamp_buckets(small, medium)

    for key, value in changed.items():
        if getattr(us, key) != value:
            setattr(us, key, value)
            result.prefs_updated += 1
    # A custom folder order is the positions the folders were created in, which
    # follow the file; mark it arranged so turning the view on keeps them.
    if changed.get("folder_order") == "custom":
        us.folders_arranged = True


def _apply_profile(us: UserSettings, data: dict, result: ImportResult) -> None:
    """Basic relevance terms and the AI texts. Only set, never cleared."""
    if isinstance(data.get("basic_scoring_enabled"), bool) \
            and data["basic_scoring_enabled"] != us.basic_scoring_enabled:
        us.basic_scoring_enabled = data["basic_scoring_enabled"]
        result.profile_updated += 1

    terms = data.get("relevance_terms")
    new_terms = us.relevance_terms
    if isinstance(terms, str) and terms.strip():
        if len(terms) > TERMS_MAX_CHARS:
            result.warnings.append(f"Relevance terms longer than {TERMS_MAX_CHARS} characters, skipped")
        elif terms.strip() != (us.relevance_terms or ""):
            new_terms = terms
            result.profile_updated += 1
    # Through save_terms, like the settings form, so the backfill is due as well.
    save_terms(us, new_terms)

    profile = data.get("ai_profile")
    if isinstance(profile, str) and profile.strip() and profile.strip() != us.ai_preference_text:
        if len(profile.strip()) > PROFILE_MAX_CHARS:
            result.warnings.append(f"AI interest profile longer than {PROFILE_MAX_CHARS} characters, skipped")
        else:
            us.ai_preference_text = profile.strip()
            us.ai_preference_updated_at = datetime.now(timezone.utc)
            us.ai_preference_source = "manual"
            result.profile_updated += 1

    for key in ("ai_summary_prompt", "ai_context_prompt"):
        text = data.get(key)
        if isinstance(text, str) and text.strip() and text.strip() != getattr(us, key):
            setattr(us, key, text.strip())
            result.profile_updated += 1


def _resolve_file_scope(
    raw: Any,
    what: str,
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
) -> list[str] | None:
    """A file scope back to ids. None when it named feeds or folders and none of
    them are here: an empty scope means every feed, so the item would silently
    turn into something much wider than it was."""
    wanted = [item for item in _json_list(raw) if isinstance(item, str)]
    resolved = _resolve_scope(wanted, feed_url_to_id, folder_name_to_id, result)
    if wanted and not resolved:
        result.warnings.append(f"{what}: none of its feeds or folders were found, skipped")
        return None
    return resolved


async def _import_saved_searches(
    user: User,
    data: list,
    labels: _LabelBook,
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
    db: AsyncSession,
) -> None:
    taken = {s.name.lower() for s in await list_saved_searches(db, user.id)}
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("params"), dict):
            continue
        name = " ".join(str(item.get("name") or "").split())
        if name.lower() in taken:
            result.searches_skipped += 1
            continue
        params = dict(item["params"])
        if params.get("scope_include"):
            scope = _resolve_file_scope(
                params["scope_include"], f"Saved search '{name}'",
                feed_url_to_id, folder_name_to_id, result,
            )
            if scope is None:
                result.searches_skipped += 1
                continue
            params["scope_include"] = scope
        if params.get("label_filter"):
            params["label_filter"] = await labels.tokens(params["label_filter"], db)
        await db.commit()  # the labels, before a rejected search rolls back
        try:
            await create_saved_search(db, user.id, name=name, params=params)
            await db.commit()
        except SavedSearchError as exc:
            await db.rollback()
            result.searches_skipped += 1
            result.warnings.append(f"Saved search '{name}' skipped: {exc}")
            continue
        taken.add(name.lower())
        result.searches_added += 1


async def _import_catchups(
    user: User,
    data: list,
    labels: _LabelBook,
    feed_url_to_id: dict[str, int],
    folder_name_to_id: dict[str, int],
    result: ImportResult,
    db: AsyncSession,
) -> None:
    """Catch-up configurations, with their briefing schedules switched off.

    A briefing emails its recipients on its own, so a restored one waits for the
    user to switch it on again rather than starting to send from a new instance.
    """
    existing = (await db.execute(
        select(UserCatchupConfig.name, UserCatchupConfig.period)
        .where(UserCatchupConfig.user_id == user.id)
    )).all()
    taken = {(name, period) for name, period in existing}
    briefings_off = 0

    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:100]
        if not name:
            continue
        period = item.get("period") if item.get("period") in _CATCHUP_PERIODS else "7days"
        if (name, period) in taken:
            result.catchups_skipped += 1
            continue
        scope = _resolve_file_scope(
            item.get("scope_include"), f"Catch-up '{name}'", feed_url_to_id, folder_name_to_id, result,
        )
        if scope is None:
            result.catchups_skipped += 1
            continue
        label_tokens = await labels.tokens(item.get("label_filter"), db)
        score_min = item.get("score_min")
        score_ok = isinstance(score_min, (int, float)) and not isinstance(score_min, bool) \
            and 0 <= score_min <= 100
        limit = _int_or_none(item.get("article_limit"))
        prompt = item.get("custom_prompt")
        status = item.get("filter_status")

        config = UserCatchupConfig(
            user_id=user.id,
            name=name,
            period=period,
            filter_status=status if status in _CATCHUP_STATUSES else "all",
            scope_include=json.dumps(scope) if scope else None,
            label_filter=json.dumps(label_tokens) if label_tokens else None,
            filter_score_min=score_min / 100 if score_ok else None,
            article_limit=max(1, min(500, limit)) if limit is not None else 500,
            custom_prompt=prompt.strip() if isinstance(prompt, str) and prompt.strip() else None,
            include_snippet=_truthy(item.get("include_snippet", True)),
            briefing_enabled=False,
        )
        briefing = item.get("briefing")
        if isinstance(briefing, dict):
            if _apply_briefing_schedule(config, briefing):
                if _truthy(briefing.get("enabled")):
                    briefings_off += 1
            else:
                result.warnings.append(f"Catch-up '{name}': briefing schedule not valid, left out")
        db.add(config)
        await db.commit()
        taken.add((name, period))
        result.catchups_added += 1

    if briefings_off:
        result.warnings.append(
            f"{briefings_off} briefing(s) were imported switched off. "
            f"Switch them on in Catch me up when you want the emails to start."
        )


def _apply_briefing_schedule(config: UserCatchupConfig, briefing: dict) -> bool:
    """Copy a valid schedule onto the config (still switched off). False if not valid."""
    interval = briefing.get("interval")
    if interval not in ("daily", "weekly"):
        return False
    day = _int_or_none(briefing.get("day"))
    if interval == "weekly" and (day is None or not 0 <= day <= 6):
        return False
    time_str = str(briefing.get("time") or "")
    if not _TIME_RE.match(time_str):
        return False
    recipients = [r.strip() for r in _json_list(briefing.get("recipients")) if isinstance(r, str)]
    if len(recipients) > 5 or not all(is_valid_email(r) for r in recipients):
        return False
    config.briefing_interval = interval
    config.briefing_day = day if interval == "weekly" else None
    config.briefing_time = time_str
    config.briefing_recipients = json.dumps(recipients) if recipients else None
    return True

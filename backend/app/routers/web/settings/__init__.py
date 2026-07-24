"""Web settings routes, split by area behind one aggregate ``router``.

``main.py`` includes this single router; each sub-module owns one thematic area
(feeds, scrape, folders, filters, tokens, profile, preferences, opml, ai, stats,
labels) and shares helpers via ``common``.
"""
from fastapi import APIRouter

from . import (
    ai,
    feeds,
    filters,
    folders,
    labels,
    nav,
    opml,
    preferences,
    profile,
    scrape,
    stats,
    tokens,
)

router = APIRouter()
router.include_router(nav.router)
router.include_router(labels.router)
router.include_router(feeds.router)
router.include_router(scrape.router)
router.include_router(folders.router)
router.include_router(filters.router)
router.include_router(tokens.router)
router.include_router(profile.router)
router.include_router(preferences.router)
router.include_router(opml.router)
router.include_router(ai.router)
router.include_router(stats.router)

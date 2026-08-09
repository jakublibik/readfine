"""Web routes for the main application UI, split by area behind one aggregate
``router``.

``main.py`` includes this single router; each sub-module owns one thematic area
(shell, articles, ai, catchup, feedback, share) and shares helpers via ``common``.
"""
from fastapi import APIRouter

from . import ai, articles, catchup, feedback, media, share, shell

router = APIRouter()
router.include_router(shell.router)
router.include_router(articles.router)
router.include_router(ai.router)
router.include_router(catchup.router)
router.include_router(feedback.router)
router.include_router(media.router)
router.include_router(share.router)

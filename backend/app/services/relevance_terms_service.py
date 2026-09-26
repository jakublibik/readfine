"""Saving the basic relevance term list, and when to invite a reader to write one.

Shared by Settings → Relevance and the /welcome screen, so a list typed at
signup is stored exactly like one typed later, backfill included.
"""
from datetime import datetime, timezone

from app.models.user import UserSettings
from app.services.relevance_service import parse_terms

TERMS_MAX_CHARS = 5000


def save_terms(s: UserSettings, text: str | None, source: str = "manual") -> None:
    """Store the list as written; a change also makes the 7-day backfill due."""
    text = (text or "").strip() or None
    if text != s.relevance_terms:
        s.relevance_terms = text
        s.relevance_terms_updated_at = datetime.now(timezone.utc)
        s.relevance_terms_source = source
    # Switched off or emptied: the scores already written stay, but the account
    # counts as never caught up, so switching back on with the same list rescores
    # the last 7 days (the articles that arrived meanwhile have no score).
    if not (s.basic_scoring_enabled and parse_terms(s.relevance_terms)):
        s.lexical_backfill_at = None


def show_relevance_intro(s: UserSettings | None) -> bool:
    """Whether the article list offers to set up basic relevance.

    Only while there is nothing to score with, and not for someone who switched
    basic scoring off or closed the bar. The caller also holds it back until the
    list has articles in it: a reader who skipped the question on /welcome and
    has no feeds yet is better served by the "add your first feed" box.
    """
    if s is None or not s.basic_scoring_enabled or s.relevance_intro_dismissed_at:
        return False
    return not parse_terms(s.relevance_terms)

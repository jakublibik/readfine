"""Per-request context carried via ``ContextVar`` for use in templates.

Lets templates that don't receive ``user`` in their render context (e.g. the
settings pages) still reflect the current viewer without threading ``user``
through every route. Set in ``app.auth.dependencies``.
"""

from contextvars import ContextVar

current_viewer_is_admin: ContextVar[bool] = ContextVar(
    "current_viewer_is_admin", default=False
)

# Whether the current viewer has an unresolved AI error (``last_ai_error`` set on
# their settings). Drives the red dot on the user menu / Settings → AI nav.
# Self-clears once a background AI call succeeds (see ``ai_scoring_service``).
current_viewer_ai_error: ContextVar[bool] = ContextVar(
    "current_viewer_ai_error", default=False
)

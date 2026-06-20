"""Per-request context carried via ``ContextVar`` for use in templates.

Lets templates that don't receive ``user`` in their render context (e.g. the
settings pages) still reflect the current viewer without threading ``user``
through every route. Set in ``app.auth.dependencies``.
"""

from contextvars import ContextVar

current_viewer_is_admin: ContextVar[bool] = ContextVar(
    "current_viewer_is_admin", default=False
)

from datetime import datetime
from pydantic import BaseModel, Field


class ArticleStateUpdate(BaseModel):
    is_read: bool | None = None
    is_starred: bool | None = None
    is_archived: bool | None = None


class ArticleStateResponse(BaseModel):
    is_read: bool
    is_starred: bool
    is_archived: bool
    read_at: datetime | None

    model_config = {"from_attributes": True}


class ArticleListItem(BaseModel):
    id: int
    feed_id: int | None
    feed_title: str | None  # resolved from Feed or UserFeed.custom_title
    url: str | None
    title: str
    author: str | None
    summary: str | None
    snippet: str | None  # pre-computed: summary or stripped content prefix
    # "this article will never have a body", not "it has none right now" — an
    # extraction still in flight is not permanently empty. Computed when the list
    # renders, so a background extraction finishing afterwards leaves it stale.
    body_permanently_empty: bool = False
    # A first extraction attempt is still in flight. Only a saved-by-URL article's
    # row cares: it was inserted with a placeholder title and learns the real one
    # when extraction finishes, so the row polls until then.
    readable_active: bool = False
    # The article carries no text of its own: no extracted body, no feed content and
    # not even the page's own description. Deliberately about what there is to show
    # rather than about extraction having failed, because a saved-by-URL row only
    # needs marking while it is still nothing but the pasted address. One that failed
    # extraction but came back with a real title and a description reads like any
    # other row, and a warning on it would be noise; the reason and the retry live in
    # the article itself either way.
    nothing_to_show: bool = False
    published_at: datetime | None
    # Display-only string, formatted per the viewer's number/date format profile
    # (order/separators vary). Parse `published_at` (ISO) for machine use.
    formatted_date: str
    estimated_read_min: int | None
    image_url: str | None
    # state (None = no UserArticleState row yet = unread, not starred)
    is_read: bool
    is_starred: bool
    is_archived: bool
    is_saved: bool = False
    ai_score: float | None = None
    labels: list[dict] = []  # [{"id": int, "name": str, "color": str}]
    # coalesce(published_at, fetched_at) used for keyset pagination cursor;
    # excluded from API JSON (internal pagination concern only)
    sort_ts: datetime | None = Field(default=None, exclude=True)

    model_config = {"from_attributes": False}


class ArticleResponse(BaseModel):
    id: int
    feed_id: int | None
    feed_title: str | None
    url: str | None
    title: str
    author: str | None
    content: str | None
    content_source: str | None
    # The page's own og:description, captured for feedless (saved) articles. Shown as
    # a clearly-marked fallback when extraction produced nothing to read.
    summary: str | None = None
    readable_content: str | None
    readable_status: str
    readable_error: str | None = None
    # True only while a first extraction attempt is in flight (status 'pending',
    # no retries yet). Drives the "Extracting…" spinner + poll; a pending article
    # waiting on retry-backoff is not "active" and must not poll/flash.
    readable_active: bool = False
    published_at: datetime | None
    estimated_read_min: int | None
    word_count: int | None
    image_url: str | None
    is_read: bool
    is_starred: bool
    is_archived: bool
    # Saved by URL by this user. Gates the "Remove from Saved" actions — deliberately
    # keyed on the article's own state, not on which view the reader came from.
    is_saved: bool = False
    read_at: datetime | None
    share_token: str | None = None
    ai_summary: str | None = None
    ai_summary_truncated: bool = False
    ai_context: str | None = None
    labels: list[dict] = []

    model_config = {"from_attributes": False}

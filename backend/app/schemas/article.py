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
    published_at: datetime | None
    formatted_date: str  # pre-formatted: HH:MM for today, "DD.MM. HH:MM" otherwise
    estimated_read_min: int | None
    image_url: str | None
    # state (None = no UserArticleState row yet = unread, not starred)
    is_read: bool
    is_starred: bool
    is_archived: bool
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
    read_at: datetime | None
    share_token: str | None = None
    ai_summary: str | None = None
    ai_context: str | None = None
    labels: list[dict] = []

    model_config = {"from_attributes": False}

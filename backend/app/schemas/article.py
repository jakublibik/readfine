from datetime import datetime
from pydantic import BaseModel


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
    title: str
    author: str | None
    summary: str | None
    published_at: datetime | None
    estimated_read_min: int | None
    image_url: str | None
    # state (None = no UserArticleState row yet = unread, not starred)
    is_read: bool
    is_starred: bool
    is_archived: bool

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
    published_at: datetime | None
    estimated_read_min: int | None
    word_count: int | None
    image_url: str | None
    is_read: bool
    is_starred: bool
    is_archived: bool
    read_at: datetime | None

    model_config = {"from_attributes": False}

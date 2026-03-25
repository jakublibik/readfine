from datetime import datetime
from pydantic import BaseModel, field_validator


class FolderCreate(BaseModel):
    name: str
    position: int = 0

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Folder name cannot be empty")
        return v


class FolderUpdate(BaseModel):
    name: str | None = None
    position: int | None = None

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("Folder name cannot be empty")
        return v


class FolderResponse(BaseModel):
    id: int
    name: str
    position: int
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedSubscribeRequest(BaseModel):
    url: str
    folder_id: int | None = None
    custom_title: str | None = None
    fetch_auth_user: str | None = None
    fetch_auth_pass: str | None = None

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Feed URL cannot be empty")
        return v


class FeedResponse(BaseModel):
    id: int
    feed_url: str
    site_url: str | None
    title: str
    favicon_url: str | None
    status: str
    last_fetched_at: datetime | None
    last_error: str | None
    subscriber_count: int
    feed_type: str

    model_config = {"from_attributes": True}


class UserFeedResponse(BaseModel):
    id: int
    feed_id: int
    folder_id: int | None
    custom_title: str | None
    extract_readable: bool
    unread_count: int
    position: int
    created_at: datetime
    feed: FeedResponse

    model_config = {"from_attributes": True}


class UserFeedUpdate(BaseModel):
    custom_title: str | None = None
    folder_id: int | None = None
    extract_readable: bool | None = None
    purge_after_days: int | None = None
    purge_keep_count: int | None = None
    position: int | None = None
    fetch_auth_user: str | None = None
    fetch_auth_pass: str | None = None

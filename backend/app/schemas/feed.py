from datetime import datetime
from pydantic import BaseModel, Field, SecretStr, field_validator

# Ceilings of the columns these land in (SmallInteger, String(n)). Without them a
# value past the column fails in the database as a 500 instead of a 422.
_SMALLINT_MAX = 32767
_FOLDER_NAME_MAX = 100


def _folder_name(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("Folder name cannot be empty")
    if len(v) > _FOLDER_NAME_MAX:
        raise ValueError(f"Folder name cannot be longer than {_FOLDER_NAME_MAX} characters")
    return v


class FolderCreate(BaseModel):
    name: str
    # Left out, the folder goes to the end of the user's order rather than to the
    # front, which a 0 default would have meant once positions started counting.
    position: int | None = Field(None, ge=0, le=_SMALLINT_MAX)

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        return _folder_name(v)


class FolderUpdate(BaseModel):
    name: str | None = None
    position: int | None = Field(None, ge=0, le=_SMALLINT_MAX)

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str | None) -> str | None:
        return _folder_name(v) if v is not None else None


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
    fetch_auth_pass: SecretStr | None = None

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
    # Consecutive fetches the host refused (anti-bot 403 / bare 429). Status stays
    # "active" through those, so this is the only signal that a feed is being blocked.
    block_count: int
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
    purge_after_days: int | None = Field(None, ge=1, le=_SMALLINT_MAX)
    purge_keep_count: int | None = Field(None, ge=1, le=_SMALLINT_MAX)
    position: int | None = Field(None, ge=0, le=_SMALLINT_MAX)
    # Subscribing checks this length in the service; an update had nothing in the way.
    fetch_auth_user: str | None = Field(None, max_length=255)
    fetch_auth_pass: SecretStr | None = None

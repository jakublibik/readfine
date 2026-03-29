import json
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, field_validator

FieldType = Literal["title_or_content", "title", "content", "author", "url", "published_at"]
ScopeType = Literal["all", "feed", "folder"]
OperatorType = Literal["contains", "not_contains", "equals", "regex", "gt", "lt"]
ActionType = Literal["label", "mark_read", "star", "hide", "notify"]
MatchOperator = Literal["AND", "OR"]


class FilterConditionCreate(BaseModel):
    field: FieldType
    operator: OperatorType
    value: str
    position: int = 0


class FilterActionCreate(BaseModel):
    action_type: ActionType
    action_value: str | None = None


class FilterCreate(BaseModel):
    name: str
    is_active: bool = True
    match_operator: MatchOperator = "AND"
    position: int = 0
    stop_on_match: bool = False
    scope_type: ScopeType = "all"
    scope_feed_id: int | None = None
    scope_folder_id: int | None = None
    scope_except: list[str] = []
    conditions: list[FilterConditionCreate] = []
    actions: list[FilterActionCreate] = []


class FilterUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    match_operator: MatchOperator | None = None
    position: int | None = None
    stop_on_match: bool | None = None
    scope_type: ScopeType | None = None
    scope_feed_id: int | None = None
    scope_folder_id: int | None = None
    scope_except: list[str] | None = None
    conditions: list[FilterConditionCreate] | None = None
    actions: list[FilterActionCreate] | None = None


class FilterConditionResponse(BaseModel):
    id: int
    field: str
    operator: str
    value: str
    position: int

    model_config = {"from_attributes": True}


class FilterActionResponse(BaseModel):
    id: int
    action_type: str
    action_value: str | None

    model_config = {"from_attributes": True}


class FilterResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    match_operator: str
    position: int
    stop_on_match: bool
    scope_type: str
    scope_feed_id: int | None
    scope_folder_id: int | None
    scope_except: list[str]
    created_at: datetime
    updated_at: datetime
    conditions: list[FilterConditionResponse]
    actions: list[FilterActionResponse]

    model_config = {"from_attributes": True}

    @field_validator("scope_except", mode="before")
    @classmethod
    def parse_scope_except(cls, v):
        if v is None:
            return []
        if isinstance(v, list):
            return v
        try:
            return json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return []


class FilterTestSample(BaseModel):
    title: str
    feed_title: str


class FilterTestResult(BaseModel):
    matched_count: int
    samples: list[FilterTestSample]

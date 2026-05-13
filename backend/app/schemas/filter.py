import json
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, field_validator

FieldType = Literal["title_or_content", "title", "content", "author", "url", "published_at", "ai_score"]
OperatorType = Literal["contains", "not_contains", "equals", "regex", "gt", "lt"]
ActionType = Literal["label", "mark_read", "star", "notify"]
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
    scope_include: list[str] = []
    scope_except: list[str] = []
    conditions: list[FilterConditionCreate] = []
    actions: list[FilterActionCreate] = []


class FilterUpdate(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    match_operator: MatchOperator | None = None
    position: int | None = None
    stop_on_match: bool | None = None
    scope_include: list[str] | None = None
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


def _parse_json_list(v) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return []


class FilterResponse(BaseModel):
    id: int
    name: str
    is_active: bool
    match_operator: str
    position: int
    stop_on_match: bool
    scope_include: list[str]
    scope_except: list[str]
    created_at: datetime
    updated_at: datetime
    conditions: list[FilterConditionResponse]
    actions: list[FilterActionResponse]

    model_config = {"from_attributes": True}

    @field_validator("scope_include", "scope_except", mode="before")
    @classmethod
    def parse_json_list(cls, v):
        return _parse_json_list(v)


class FilterTestSample(BaseModel):
    title: str
    feed_title: str


class FilterTestResult(BaseModel):
    matched_count: int
    samples: list[FilterTestSample]

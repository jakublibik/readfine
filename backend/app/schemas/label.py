import re
from datetime import datetime
from pydantic import BaseModel, field_validator


class LabelCreate(BaseModel):
    name: str
    color: str = "#6366f1"
    position: int = 0

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str) -> str:
        if not re.match(r"^#[0-9a-fA-F]{6}$", v):
            raise ValueError("Color must be a hex color (#RRGGBB)")
        return v


class LabelUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    position: int | None = None

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^#[0-9a-fA-F]{6}$", v):
            raise ValueError("Color must be a hex color (#RRGGBB)")
        return v


class LabelResponse(BaseModel):
    id: int
    name: str
    color: str
    position: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ArticleLabelAssign(BaseModel):
    label_id: int

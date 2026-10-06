import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator

from app.schemas.common import SMALLINT_MAX, clean_name


class LabelCreate(BaseModel):
    name: str
    color: str = "#6366f1"
    position: int = Field(0, ge=0, le=SMALLINT_MAX)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        return clean_name(v, "Label")

    @field_validator("color")
    @classmethod
    def validate_color(cls, v: str) -> str:
        if not re.match(r"^#[0-9a-fA-F]{6}$", v):
            raise ValueError("Color must be a hex color (#RRGGBB)")
        return v


class LabelUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    position: int | None = Field(None, ge=0, le=SMALLINT_MAX)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        return clean_name(v, "Label") if v is not None else None

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

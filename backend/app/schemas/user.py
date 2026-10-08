from datetime import datetime
from pydantic import BaseModel, EmailStr


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserResponse(BaseModel):
    id: int
    email: str
    display_name: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}

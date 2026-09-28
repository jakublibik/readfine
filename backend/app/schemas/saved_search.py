from datetime import datetime
from typing import Any

from pydantic import BaseModel


class SavedSearchResponse(BaseModel):
    id: int
    name: str
    # The stored search, keyed like the search's query string; only the keys that
    # are set appear. Open it with GET /articles?view_id={id}.
    params: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

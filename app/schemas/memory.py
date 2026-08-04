from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import StrictResponse


class MemoryFactResponse(StrictResponse):
    id: uuid.UUID
    kind: str
    text: str
    predicate: str | None = None
    event_time: datetime | None = None


class MemoryListResponse(StrictResponse):
    items: list[MemoryFactResponse]


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=200)
    from_date: date | None = None
    to_date: date | None = None

    @model_validator(mode="after")
    def valid_window(self):
        if self.from_date and self.to_date and self.from_date > self.to_date:
            raise ValueError("from_date must be on or before to_date")
        return self


class MemorySearchItem(StrictResponse):
    id: uuid.UUID
    kind: Literal["fact", "insight"]
    text: str
    observed_at: datetime | None = None
    similarity: float = Field(ge=-1.0, le=1.0)


class MemorySearchResponse(StrictResponse):
    items: list[MemorySearchItem]


class MemoryForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["fact", "predicate", "all"]
    fact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    predicate: str | None = Field(default=None, max_length=100)
    future_learning: Literal["allow", "block"] = "allow"
    confirm: bool

    @model_validator(mode="after")
    def valid_scope(self):
        if not self.confirm:
            raise ValueError("confirm must be true")
        if self.scope == "fact" and not self.fact_ids:
            raise ValueError("fact_ids are required for fact scope")
        if self.scope == "predicate" and not self.predicate:
            raise ValueError("predicate is required for predicate scope")
        if self.scope == "all" and (self.fact_ids or self.predicate):
            raise ValueError("all scope cannot include fact_ids or predicate")
        return self


class MemoryForgetResponse(StrictResponse):
    status: str
    forgotten_fact_ids: list[uuid.UUID]
    invalidated_insight_ids: list[uuid.UUID]
    memory_generation: int | None = None

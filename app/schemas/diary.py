"""일기 목록·상세 응답 스키마."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictResponse, UtcDatetime

DiaryType = Literal["personal", "moly"]
Weather = Literal["sunny", "cloudy", "rainy", "windy"]


class DiaryListItem(StrictResponse):
    id: UUID
    diary_date: date
    type: DiaryType
    title: str | None
    weather: Weather
    preview: str = Field(max_length=60)
    published_at: UtcDatetime
    read: bool


class DiaryListResponse(StrictResponse):
    data: list[DiaryListItem]
    next_cursor: date | None


class ConversationRef(StrictResponse):
    anchor_date: date


class DiaryDetailResponse(StrictResponse):
    id: UUID
    diary_date: date
    type: DiaryType
    title: str | None
    weather: Weather
    body: str
    conversation_ref: ConversationRef | None
    published_at: UtcDatetime
    first_read_at: UtcDatetime | None

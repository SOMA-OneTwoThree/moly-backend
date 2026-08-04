"""일기 목록·상세 응답 스키마."""
from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.schemas.common import StrictResponse, UtcDatetime

DiaryType = Literal["personal", "moly"]
DiaryKind = Literal["welcome", "shared_day", "capi_day"]
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


class DiaryListItemV2(StrictResponse):
    """의미가 모호한 legacy ``type`` 대신 캐피 일기의 실제 종류를 노출한다."""

    id: UUID
    display_date: date
    kind: DiaryKind
    author: Literal["capi"] = "capi"
    title: str | None
    weather: Weather
    preview: str = Field(max_length=60)
    published_at: UtcDatetime
    read: bool


class DiaryListResponseV2(StrictResponse):
    data: list[DiaryListItemV2]
    # ``v1.<base64url>`` 형식의 불투명 (display_date, id) keyset cursor.
    next_cursor: str | None = Field(default=None, max_length=256)


class DiaryDetailResponseV2(StrictResponse):
    id: UUID
    display_date: date
    kind: DiaryKind
    author: Literal["capi"] = "capi"
    title: str | None
    weather: Weather
    body: str
    occurred_at: UtcDatetime | None
    conversation_ref: ConversationRef | None
    published_at: UtcDatetime
    first_read_at: UtcDatetime | None

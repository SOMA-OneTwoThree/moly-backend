"""오늘의 글귀 API 계약."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field

from app.schemas.common import StrictResponse

Locale = Literal["ko", "en", "ja"]


class DailyAffirmationItem(StrictResponse):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=200)


class DailyAffirmationResponse(StrictResponse):
    local_date: date
    locale: Locale
    affirmation: DailyAffirmationItem

"""오늘의 글귀 API 계약."""

from __future__ import annotations

from datetime import date
from typing import Literal

from app.schemas.common import StrictResponse

Locale = Literal["ko", "en", "ja"]


class DailyAffirmationResponse(StrictResponse):
    local_date: date
    locale: Locale
    text: str

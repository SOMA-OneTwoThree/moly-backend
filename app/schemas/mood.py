"""감정 종류는 클라이언트가 확장할 수 있는 자유 문자열이다."""
from datetime import date

from pydantic import BaseModel

from app.schemas.common import StrictResponse


class MoodPutRequest(BaseModel):
    kind: str
    note: str = ""


class MoodEntryResponse(StrictResponse):
    date: date
    kind: str
    note: str


class MoodListResponse(StrictResponse):
    data: list[MoodEntryResponse]

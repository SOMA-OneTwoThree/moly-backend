"""Conversation diary recall rendering contract."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app.services import recall_diaries


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    async def scalar(self, stmt, params=None):
        return "승민"

    async def execute(self, stmt, params=None):
        return _Rows(
            [
                {
                    "id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
                    "kind": "welcome",
                    "display_date": date(2026, 8, 4),
                    "title": "{유저이름}, 첫 만남",
                    "content": "오늘 {유저이름}과 처음 대화를 나눴다.",
                    "weather": "sunny",
                    "published_at": datetime(2026, 8, 4, tzinfo=timezone.utc),
                    "first_read_at": None,
                    "exact_count": 1,
                }
            ]
        )


async def test_recall_renders_welcome_placeholder_at_egress() -> None:
    result = await recall_diaries.recall(
        _Session(),
        uuid.UUID("11111111-1111-1111-1111-111111111111"),
        query="첫 만남",
        need="full",
    )
    item = result["items"][0]
    assert item["title"] == "승민, 첫 만남"
    assert item["body"] == "오늘 승민과 처음 대화를 나눴다."
    assert "{유저이름}" not in item["excerpt"]

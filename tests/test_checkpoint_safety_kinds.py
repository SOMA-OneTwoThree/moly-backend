"""요약 저장소의 위기 종류 차단 — 최근 원문 조회와 다른 계약이다."""
from types import SimpleNamespace
import uuid

import pytest

from app.services import checkpoint_repo


@pytest.mark.parametrize("after_id", [None, 10])
async def test_summary_range_never_returns_safety_or_fortune_source(after_id):
    rows = [
        {"id": idx, "sender": sender, "kind": kind, "content": kind, "created_at": None}
        for idx, (kind, sender) in enumerate(
            [
                ("normal", "user"), ("normal", "moly"), ("greeting", "moly"),
                ("safety", "user"), ("safety", "moly"),
                ("safety_check", "user"), ("safety_check", "moly"),
                ("fortune_context_root", "user"), ("fortune_derived", "moly"),
            ],
            start=11,
        )
    ]
    calls = []

    async def execute(statement, params):
        calls.append((str(statement), params))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: rows))

    user_id = uuid.uuid4()
    result = await checkpoint_repo.load_range(
        SimpleNamespace(execute=execute), user_id, after_id=after_id, through_id=30,
    )

    assert [message.id for message in result] == [11, 12, 13]
    sql, params = calls[0]
    assert "user_id = :user_id" in sql
    assert params["user_id"] == user_id
    assert "'safety_check'" in sql and "'safety'" in sql

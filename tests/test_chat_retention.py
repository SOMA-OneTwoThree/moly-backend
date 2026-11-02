"""원문은 완료된 v5 범위만 제외하고 개수 제한으로 자르지 않는다."""
from datetime import date
from types import SimpleNamespace
import uuid

import pytest

from app.core.errors import AppError
from app.services import chat
from tests.test_chat import FakeSession, _Result


UID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def messages(count, size=1, start=1):
    return [SimpleNamespace(
        id=i, sender="user" if i % 2 else "moly", content="x" * size,
        kind="normal", activity_date=date(2026, 11, 2), created_at=None,
    ) for i in range(start, start + count)]


class Pages(FakeSession):
    def __init__(self, rows):
        super().__init__()
        descending = list(reversed(rows))
        self.pages = [descending[i:i + 120] for i in range(0, len(rows), 120)]
        self.queries = []

    async def execute(self, statement, params=None):
        self.queries.append(statement.compile().params)
        return _Result(self.pages.pop(0) if self.pages else [])


async def test_small_messages_continue_across_query_page_limit():
    session = Pages(messages(121))
    result = await chat._context(session, UID, 0, retention_enabled=True, compact_crises=False)
    assert len(result.conversation) == 121
    assert len(session.queries) == 2
    assert result.new_anchor is None
    assert not result.incomplete


async def test_soft_threshold_requests_work_without_dropping_raw_history():
    rows = messages(40, size=100)
    result = await chat._context(Pages(rows), UID, 0, retention_enabled=True, compact_crises=False)
    assert len(result.conversation) == 40
    assert result.checkpoint_keep_from is not None
    assert result.new_anchor is None


async def test_unprocessed_raw_pressure_refuses_instead_of_publishing_a_short_tail():
    with pytest.raises(AppError) as error:
        await chat._context(Pages(messages(20, size=1000)), UID, 0,
                            retention_enabled=True, compact_crises=False)
    assert error.value.code == "AI_UNAVAILABLE"


async def test_v5_read_boundary_does_not_inherit_a_later_legacy_anchor():
    session = Pages(messages(4, start=7))
    result = await chat._context(session, UID, 1000, checkpoint_through=6,
                                 retention_enabled=True, compact_crises=False)
    assert len(result.conversation) == 4
    assert 7 in session.queries[0].values()


async def test_new_guard_keeps_safety_messages_in_recent_raw_history():
    rows = messages(2)
    rows[0].kind = "safety"
    rows[1].kind = "safety_check"
    result = await chat._context(Pages(rows), UID, 0, retention_enabled=True, compact_crises=False)
    assert len(result.conversation) == 2

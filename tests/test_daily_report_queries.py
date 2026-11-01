from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from worker import daily_report as report
from tests.test_daily_report import NOW


class QueryError(Exception):
    def __init__(self, code):
        self.sqlstate = code
        super().__init__("private-query-and-credentials")


async def test_timeout_retries_with_fresh_session_and_safe_logs(monkeypatch, caplog):
    opened, closed = [], []
    @asynccontextmanager
    async def session():
        attempt = len(opened)
        opened.append(attempt)
        async def execute(query, params=None):
            if params is not None:
                if attempt == 0:
                    raise QueryError("57014")
                return SimpleNamespace(mappings=lambda: SimpleNamespace(one=lambda: {"dau": 4}))
        try:
            yield SimpleNamespace(execute=execute)
        finally:
            closed.append(attempt)
    monkeypatch.setattr(report, "get_sessionmaker", lambda: session)
    value, error = await report.read_metric("activity", object(), {"now": NOW}, retry_timeout=True)
    assert value == {"dau": 4} and error is None
    assert opened == closed == [0, 1]
    assert "sqlstate=57014" in caplog.text and "retry=True" in caplog.text
    assert "private-query-and-credentials" not in caplog.text


@pytest.mark.parametrize("code,attempts", [("42883", 1), ("57014", 2)])
async def test_failed_retry_is_bounded_and_non_timeout_is_not_retried(monkeypatch, code, attempts):
    read = AsyncMock(side_effect=QueryError(code))
    monkeypatch.setattr(report, "read_row", read)
    result, error = await report.read_metric("activity", object(), {"now": NOW}, retry_timeout=True)
    assert result is None and error == ("시간 초과" if code == "57014" else "조회 실패")
    assert read.await_count == attempts


async def test_cancellation_is_not_converted_into_missing_metric(monkeypatch):
    monkeypatch.setattr(report, "read_row", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await report.read_metric("activity", object(), {"now": NOW}, retry_timeout=True)

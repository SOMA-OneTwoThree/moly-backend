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


# tests/conftest.py replaces daily_report.business for every unit test; keep the
# real function (captured at import, before that fixture runs).
business = report.business
_GUARD = "CASE WHEN p.timezone<>'' AND p.timezone<>ALL(CAST(:tz_excluded AS text[]))"
CONFIG = {"diary_min_user_chars": 60, "launch_until": None}
DIARY_ROW = {"expected": 4, "completed": 3, "deleted": 1}


def test_diaries_sql_skips_zone_catalog_and_guards_every_conversion():
    from sqlalchemy.dialects import postgresql

    sql = report._DIARIES_SQL.text
    assert "pg_timezone_names" not in sql
    assert "tz_excluded" in report._DIARIES_SQL.compile(dialect=postgresql.dialect()).params
    assert " WHERE p.timezone<>'' AND p.timezone<>ALL(CAST(:tz_excluded AS text[]))" in sql
    # Every AT TIME ZONE is inside an open CASE whose condition is the exclusion check.
    pieces = sql.split("AT TIME ZONE")
    assert len(pieces) == 4
    for before in pieces[:-1]:
        case = before.rfind("CASE WHEN p.timezone")
        assert case != -1 and before[case:].startswith(_GUARD) and " END" not in before[case:]
    assert "x.timezone" not in sql
    # The previous join query stays available as the fallback.
    assert "JOIN pg_timezone_names tz ON tz.name=p.timezone" in report._DIARIES_JOIN_SQL.text


def _reader(diary_errors=(), *, activity_errors=(), signup_errors=()):
    calls = []
    errors = {id(report._DIARIES_SQL): list(diary_errors), id(report._ACTIVITY_SQL): list(activity_errors),
              id(report._SIGNUPS_SQL): list(signup_errors)}

    async def read_row(query, params):
        calls.append((query, params))
        pending = errors.get(id(query), [])
        if pending:
            raise pending.pop(0)
        return DIARY_ROW if query in (report._DIARIES_SQL, report._DIARIES_JOIN_SQL) else {"value": 1}
    return read_row, calls


@pytest.mark.parametrize("given,expected", [
    (["Not/AZone", 3, None, "Not/AZone", "Bad/Zone"], ["Bad/Zone", "Not/AZone"]),
    ([], []), (None, []), ("Not/AZone", []),
])
async def test_business_binds_unresolvable_zones_as_list(monkeypatch, given, expected):
    read_row, calls = _reader()
    monkeypatch.setattr(report, "read_row", read_row)
    config = dict(CONFIG) if given is None else {**CONFIG, "tz_unresolvable": given}
    data = await business(NOW, config)
    diary_params = next(params for query, params in calls if query is report._DIARIES_SQL)
    assert diary_params["tz_excluded"] == expected
    assert data["diaries"] == DIARY_ROW and data["errors"] == {}


@pytest.mark.parametrize("first", [QueryError("57014"), TimeoutError()])
async def test_diaries_retry_once_after_timeout(monkeypatch, first):
    read_row, calls = _reader([first])
    monkeypatch.setattr(report, "read_row", read_row)
    data = await business(NOW, CONFIG)
    assert [query for query, _ in calls if query is report._DIARIES_SQL] == [report._DIARIES_SQL] * 2
    assert data["diaries"] == DIARY_ROW and "diaries" not in data["errors"]


async def test_diaries_timeout_retry_is_bounded(monkeypatch):
    read_row, calls = _reader([TimeoutError(), QueryError("57014"), TimeoutError()])
    monkeypatch.setattr(report, "read_row", read_row)
    data = await business(NOW, CONFIG)
    assert sum(query is report._DIARIES_SQL for query, _ in calls) == 2
    assert data["diaries"] is None and data["errors"]["diaries"] == "시간 초과"


async def test_activity_also_retries_after_outer_timeout_but_signups_do_not(monkeypatch):
    read_row, calls = _reader(activity_errors=[TimeoutError()], signup_errors=[TimeoutError()])
    monkeypatch.setattr(report, "read_row", read_row)
    data = await business(NOW, CONFIG)
    assert sum(query is report._ACTIVITY_SQL for query, _ in calls) == 2
    assert sum(query is report._SIGNUPS_SQL for query, _ in calls) == 1
    assert data["activity"] == {"value": 1} and data["errors"] == {"signups": "시간 초과"}


async def test_unknown_postgres_zone_falls_back_to_join_query_once(monkeypatch, caplog):
    read_row, calls = _reader([QueryError("22023")])
    monkeypatch.setattr(report, "read_row", read_row)
    data = await business(NOW, CONFIG)
    diaries = [query for query, _ in calls if query in (report._DIARIES_SQL, report._DIARIES_JOIN_SQL)]
    assert diaries == [report._DIARIES_SQL, report._DIARIES_JOIN_SQL]
    assert data["diaries"] == DIARY_ROW and data["errors"] == {}
    assert "sqlstate=22023" in caplog.text and "outcome=fallback" in caplog.text
    assert "private-query-and-credentials" not in caplog.text


async def test_join_fallback_runs_only_once(monkeypatch):
    read = AsyncMock(side_effect=QueryError("22023"))
    monkeypatch.setattr(report, "read_row", read)
    result, error = await report.read_metric("diaries", report._DIARIES_SQL, {"now": NOW},
                                             retry_timeout=True, fallback=report._DIARIES_JOIN_SQL)
    assert result is None and error == "조회 실패"
    assert [call.args[0] for call in read.await_args_list] == [report._DIARIES_SQL, report._DIARIES_JOIN_SQL]


async def test_zone_error_without_fallback_is_not_retried(monkeypatch):
    read = AsyncMock(side_effect=QueryError("22023"))
    monkeypatch.setattr(report, "read_row", read)
    result, error = await report.read_metric("activity", object(), {"now": NOW}, retry_timeout=True)
    assert result is None and error == "조회 실패" and read.await_count == 1

"""Safe diagnostics with the unchanged real report retry policy, no DB/Slack calls."""

import asyncio
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from scripts import check_daily_report as report
from scripts import chat_crypto_release_workflow as workflow
from worker import daily_report

_REAL_BUSINESS = daily_report.business
SHA = "a" * 40
CANARY = "synthetic-raw-body-and-key-and-cipher"
MESSAGE = "어제 신규 가입: 0\nDAU: 0\nMAU: 0\n현재 구독 이용자: 0\n개인 일기: 대상 0"


class Context:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def fake_report(monkeypatch):
    monkeypatch.setattr(report.settings, "environment", "development")
    monkeypatch.setattr(report.settings, "git_sha", SHA)
    monkeypatch.setattr(report.db, "_engine", None)
    monkeypatch.setattr(report.db, "get_sessionmaker", lambda: Context)
    engine = SimpleNamespace(dispose=AsyncMock())

    def make_engine(*args, **kwargs):
        factory(*args, **kwargs)
        return engine

    # Use a synchronous recorder: engine construction itself does no I/O.
    from unittest.mock import Mock

    factory = Mock()
    monkeypatch.setattr(report, "create_async_engine", make_engine)
    monkeypatch.setattr(report.monitoring, "rc_snapshot", AsyncMock(return_value={}))
    monkeypatch.setattr(report.monitoring, "_daily_message", AsyncMock(return_value=MESSAGE))
    return engine, factory


async def test_success_stdout_contract_and_readonly_budgets_are_unchanged(fake_report, capsys):
    engine, factory = fake_report
    await report.check("development", SHA)
    value = json.loads(capsys.readouterr().out)
    assert set(value) == {
        "daily_report",
        "environment",
        "version",
        "read_only",
        "slack_sent",
        "elapsed_ms",
    }
    assert (
        value["daily_report"] == "ok"
        and value["read_only"] is True
        and value["slack_sent"] is False
    )
    assert factory.call_args.kwargs["connect_args"]["server_settings"] == {
        "default_transaction_read_only": "on"
    }
    assert factory.call_args.kwargs["hide_parameters"] is True
    assert report.monitoring._MONITORING_BUDGET_S == 15.0
    engine.dispose.assert_awaited_once()
    report.monitoring._daily_message.assert_awaited_once()
    assert report.monitoring._daily_message.call_args.kwargs["persist_snapshot"] is False


async def test_cold_activity_timeouts_and_fallback_keep_typed_two_attempt_evidence(
    fake_report, monkeypatch
):
    attempts = 0

    async def row(query, params):
        nonlocal attempts
        if query is daily_report._ACTIVITY_SQL:
            attempts += 1
            original = RuntimeError(CANARY)
            original.sqlstate = "57014"
            raise DBAPIError("SELECT " + CANARY, {"body": CANARY}, original)
        return {}

    monkeypatch.setattr(daily_report, "read_row", row)

    async def message(now, rc, *, persist_snapshot):
        result = await _REAL_BUSINESS(now, {"diary_min_user_chars": 0, "launch_until": now})
        assert result["errors"] == {"activity": "시간 초과"}
        return MESSAGE + "\n집계 시간 초과"

    monkeypatch.setattr(report.monitoring, "_daily_message", message)
    before = list(daily_report._log.handlers)
    with pytest.raises(report.DailyReportCheckError) as error:
        await report.check("development", SHA)
    value = report.safe_report_result(error.value.result, environment="development", version=SHA)
    activity = [r for r in value["failures"] if r["metric"] == "activity"]
    assert attempts == 2 and [r["attempt"] for r in activity] == [1, 2]
    assert [r["retry"] for r in activity] == [True, False]
    assert all(
        r["exception"] == "DBAPIError" and r["sqlstate"] == "57014" and r["phase"] == "metric"
        for r in activity
    )
    assert value["failures"][-1]["phase"] == "validation" and value["error"] == "ValueError"
    assert CANARY not in json.dumps(value) and CANARY not in str(error.value)
    assert daily_report._log.handlers == before


async def test_rc_failure_has_phase_and_sqlstate_without_exception_text(fake_report, monkeypatch):
    error = RuntimeError(CANARY)
    error.sqlstate = "08006"
    monkeypatch.setattr(report.monitoring, "rc_snapshot", AsyncMock(side_effect=error))
    with pytest.raises(report.DailyReportCheckError) as caught:
        await report.check("development", SHA)
    row = caught.value.result["failures"][0]
    assert (
        row["phase"] == "rc_snapshot"
        and row["exception"] == "RuntimeError"
        and row["sqlstate"] == "08006"
    )
    assert CANARY not in json.dumps(caught.value.result)


async def test_cancellation_disposes_own_engine_and_removes_capture(fake_report, monkeypatch):
    entered = asyncio.Event()

    async def rc(*args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(report.monitoring, "rc_snapshot", rc)
    handlers = list(daily_report._log.handlers)
    task = asyncio.create_task(report.check("development", SHA))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake_report[0].dispose.assert_awaited_once()
    assert daily_report._log.handlers == handlers


def test_capture_uses_only_exact_typed_template_and_bounds():
    capture = report.MetricFailureCapture()

    def record(metric="activity", exception="DBAPIError", state="57014"):
        return logging.LogRecord(
            "worker.daily_report",
            logging.WARNING,
            "x.py",
            1,
            report._LOG_TEMPLATE,
            (metric, datetime.now(timezone.utc), exception, state, 2501, 1, True),
            None,
        )

    for _ in range(20):
        capture.emit(record())
    assert len(capture.rows) == 16 and capture.truncated is True
    for bad in (record(CANARY), record(exception=CANARY), record(state=CANARY), record(metric=[])):
        isolated = report.MetricFailureCapture()
        isolated.emit(bad)
        assert isolated.rows == []
    assert CANARY not in json.dumps(capture.rows)


@pytest.mark.parametrize(
    "mutation", ["extra", "exception", "metric", "sqlstate", "attempt", "retry", "elapsed", "phase"]
)
def test_cli_and_workflow_parsers_reject_untrusted_fields(mutation):
    row = {
        "phase": "metric",
        "metric": "activity",
        "exception": "DBAPIError",
        "sqlstate": "57014",
        "elapsed_ms": 2501,
        "attempt": 1,
        "retry": True,
    }
    value = {
        "daily_report": "failed",
        "environment": "development",
        "version": SHA,
        "read_only": True,
        "slack_sent": False,
        "elapsed_ms": 3001,
        "error": "ValueError",
        "diagnostic_format": report.DIAGNOSTIC_FORMAT,
        "failures": [row],
        "diagnostics_truncated": False,
    }
    if mutation == "extra":
        value["raw_sql"] = CANARY
    elif mutation == "attempt":
        row[mutation] = True
    elif mutation == "retry":
        row[mutation] = CANARY
    elif mutation == "elapsed":
        row["elapsed_ms"] = -1
    else:
        row[mutation] = CANARY
    with pytest.raises(ValueError):
        report.safe_report_result(value, environment="development", version=SHA)
    with pytest.raises(ValueError):
        workflow.safe_report_metadata(value, "development", SHA)


def test_validator_enums_match_without_importing_app_into_gha_program():
    assert workflow.REPORT_PHASES == report.PHASES
    assert workflow.REPORT_METRICS == report.METRICS
    assert workflow.REPORT_EXCEPTIONS == report.EXCEPTIONS
    assert "from worker" not in workflow.REMOTE and "from app" not in workflow.REMOTE

"""범위가 닫힌 약속 정리 dead 표시. DB 없이 호출만 확인한다(실제 SQL은 DB에서 따로 확인)."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import contract_recovery as recovery
from app.services import jobs, monitoring_state
from worker import monitoring

NOW = datetime(2026, 11, 6, 6, tzinfo=timezone.utc)


def test_mark_sql_only_closes_same_generation_ranges_the_cursor_has_passed():
    sql = str(recovery._MARK)
    for clause in (
        "j.state = 'dead'", "j.job_type = 'contract_compile'", "j.queue = 'memory'",
        "j.result_code IS NULL", "j.payload_redacted_at IS NULL",
        "j.payload->'retention_enabled' = 'true'::jsonb",
        "(j.payload->>'retention_preparation_id') IS NULL",
        "cc.contract_generation = cc.memory_generation",
        "FOR UPDATE OF j SKIP LOCKED",
        "WHERE j.id = c.id AND j.state = 'dead' AND j.result_code IS NULL",
    ):
        assert clause in sql
    assert f"cc.memory_generation = {recovery._GEN}" in sql
    assert f"cc.contract_processed_through_message_id >= {recovery._THROUGH}" in sql
    # 처리기가 잘못된 payload로 죽이는 through_message_id<=0은 닫힌 범위로 보지 않는다.
    assert f"{recovery._THROUGH} > 0" in sql
    # 숫자 변환은 CASE 안에서만 한다.
    assert "THEN (j.payload->>'through_message_id')::bigint END" in recovery._THROUGH


async def test_mark_closed_sets_marker_and_counts_rows():
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [("a",), ("b",)])))
    assert await recovery.mark_closed(session) == 2
    stmt, params = session.execute.await_args.args
    assert stmt is recovery._MARK
    assert params == {"marker": "contract_range_closed", "limit": 200}


def test_stats_sql_excludes_only_marked_contract_dead():
    sql = str(jobs._STATS_SQL)
    assert ("AND NOT (state = 'dead' AND j.queue = 'memory' AND j.job_type = 'contract_compile'\n"
            "           AND j.result_code IS NOT DISTINCT FROM 'contract_range_closed')") in sql
    assert recovery.RANGE_CLOSED == "contract_range_closed"
    # 요약 영수증 추가 제외(바꿔치기)가 계속 동작해야 한다.
    assert sql.count("GROUP BY queue, state") == 1


async def test_contract_observation_runs_last_and_does_not_block_daily_status(monkeypatch):
    sequence = []

    async def publish(obs, *args, **kwargs):
        sequence.append(obs.signal)

    async def summary(*args):
        sequence.append("summary")

    async def broken(*args):
        sequence.append("contract")
        raise RuntimeError("boom")

    @asynccontextmanager
    async def fake_session():
        yield SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: fake_session)
    monkeypatch.setattr(monitoring, "rc_snapshot", AsyncMock(return_value={
        env: {"failed": 0, "pending": 0, "fingerprint": "empty", "oldest": None}
        for env in ("PRODUCTION", "UNKNOWN", "SANDBOX")}))
    monkeypatch.setattr(monitoring_state, "publish", publish)
    monkeypatch.setattr(monitoring, "_summary_observation", summary)
    monkeypatch.setattr(recovery, "mark_closed", broken)
    await monitoring._emit(NOW, {})  # 한국시간 15시라 일일 상태도 관측한다
    # 알림 없는 표시 작업은 일일 보고 뒤에 돌아 그 시간 예산을 쓰지 않는다.
    assert sequence.index("summary") < sequence.index("daily_status") < sequence.index("contract")
    assert sequence[-1] == "contract"


async def test_contract_observation_commits_with_statement_timeout(monkeypatch):
    fake = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())

    @asynccontextmanager
    async def fake_session():
        yield fake

    monkeypatch.setattr(monitoring, "get_sessionmaker", lambda: fake_session)
    monkeypatch.setattr(recovery, "mark_closed", AsyncMock(return_value=2))
    await monitoring._contract_observation(NOW)
    fake.commit.assert_awaited_once()
    assert "statement_timeout" in str(fake.execute.await_args_list[0].args[0])

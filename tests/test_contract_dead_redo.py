"""dead가 된 약속 정리 작업을 간격을 두고 다시 만드는 규칙(합성 세션만 사용)."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services import context_retention as retention, contract_repo, jobs

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
DEAD = uuid.UUID(int=900)


class Result:
    def __init__(self, row=None):
        self.row = row

    def first(self):
        return self.row


def dead_row(code, *, ago, depth=None, state="dead"):
    return (state, DEAD, NOW - ago, code, None if depth is None else str(depth))


@pytest.mark.parametrize("depth,expected", [
    (0, timedelta(minutes=30)), (1, timedelta(hours=1)), (2, timedelta(hours=2)),
    (3, timedelta(hours=4)), (6, timedelta(hours=24)), (40, timedelta(hours=24)),
])
def test_redo_delay_doubles_from_thirty_minutes_up_to_a_day(depth, expected):
    assert retention.contract_redo_delay(depth) == expected


def depth_of(row):
    redo = retention.contract_redo(row, now=NOW)
    return None if redo is None else (redo.depth, redo.exhausted)


@pytest.mark.parametrize("code", ["unexpected_error", "timeout", "lease_expired",
                                  "attempts_exhausted", "contract_fallback_attempts_exhausted"])
def test_transient_dead_is_redone_up_to_the_cap_with_growing_intervals(code):
    # 원본 30분 → 1회차 1시간 → 2회차 2시간 → 3회차 4시간, 4회차까지만 다시 만든다.
    for depth, wait in ((None, timedelta(minutes=30)), (1, timedelta(hours=1)),
                        (2, timedelta(hours=2)), (3, timedelta(hours=4))):
        assert depth_of(dead_row(code, ago=wait - timedelta(seconds=1), depth=depth)) is None
        assert depth_of(dead_row(code, ago=wait, depth=depth)) == ((depth or 0) + 1, False)
    assert jobs.REDO_MAX_DEPTH == 4
    # 상한에 닿은 dead는 간격을 기다리지 않고 바로 "상한"으로 본다(모델을 부르지 않는 마지막 작업).
    assert depth_of(dead_row(code, ago=timedelta(seconds=1), depth=4)) == (5, True)
    assert depth_of(dead_row(code, ago=timedelta(days=3), depth=9)) == (10, True)


@pytest.mark.parametrize("code", ["contract_retention_evidence", "contract_retention_schema",
                                  "contract_retention_length", "contract_retention_merge",
                                  "contract_retention_budget"])
def test_content_failure_dead_is_redone_once_only_for_the_original_job(code):
    # 배포 전에 이미 내용 실패로 dead가 된 원본(redo_depth 없음)은 한 번 다시 만든다.
    assert depth_of(dead_row(code, ago=timedelta(days=2))) == (1, False)
    # 다시 만든 작업이 또 내용 실패로 죽으면(마지막 시도는 진행하므로 정상이라면 없다) 상한이다.
    assert depth_of(dead_row(code, ago=timedelta(days=2), depth=1)) == (2, True)


@pytest.mark.parametrize("code", ["contract_background_budget", "contract_retention_input_budget",
                                  "invalid_contract_retention_payload", "missing_user", None])
def test_deterministic_or_unknown_dead_is_never_redone(code):
    assert depth_of(dead_row(code, ago=timedelta(days=3))) is None
    assert depth_of(dead_row(code, ago=timedelta(days=3), depth=4)) is None


def test_a_dead_no_model_skip_job_is_never_redone():
    # 상한 뒤 마지막 작업 자체가 dead가 되면 아무것도 만들지 않는다(반복 방지).
    row = dead_row("unexpected_error", ago=timedelta(days=3), depth=5) + ("true",)
    assert retention.contract_redo(row, now=NOW) is None
    plain = dead_row("unexpected_error", ago=timedelta(days=3), depth=1) + (None,)
    assert depth_of(plain) == (2, False)


def test_only_dead_rows_with_a_finish_time_are_redone():
    assert depth_of(dead_row("timeout", ago=timedelta(days=1), state="succeeded")) is None
    assert depth_of(("dead", DEAD, None, "timeout", None)) is None
    naive = ("dead", DEAD, (NOW - timedelta(hours=1)).replace(tzinfo=None), "timeout", None)
    assert depth_of(naive) == (1, False)


def test_malformed_depth_is_not_treated_as_an_original_job():
    # 형식이 깨진 깊이는 원본으로 보지 않고 상한에 닿은 것으로 본다(모델 호출 반복 없음).
    row = ("dead", DEAD, NOW - timedelta(days=2), "contract_retention_evidence", "x")
    assert depth_of(row) == (65, True)
    row = ("dead", DEAD, NOW - timedelta(minutes=1), "timeout", "-1")
    assert depth_of(row) == (65, True)


@pytest.fixture
def producer(monkeypatch):
    """enqueue_contract_processing의 비바인딩 경로. 마지막 종료 작업 행을 바꿔 끼운다."""
    uid, made, state = uuid.uuid4(), [], {"terminal": None, "queries": []}
    monkeypatch.setattr(retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(retention.context_compat, "load", AsyncMock(return_value=None))
    monkeypatch.setattr(contract_repo, "effective_contract_cursor", AsyncMock(return_value=7))
    monkeypatch.setattr(retention, "datetime", type("FixedNow", (datetime,), {
        "now": classmethod(lambda cls, tz=None: NOW)}))

    class Session:
        async def scalar(self, sql, params):
            if sql is retention._ADOPTED_USER:
                return True
            assert sql is retention._CONTRACT_ACTIVE
            return None

        async def execute(self, sql, params):
            state["queries"].append(sql)
            if sql is retention._CONTRACT_LATEST_TERMINAL:
                return Result(state["terminal"])
            pytest.fail("a dead terminal never falls through to the resume/base lookups")

    async def enqueue(session, **kwargs):
        made.append(kwargs)
        return None if any(m["dedup_key"] == kwargs["dedup_key"] for m in made[:-1]) else uuid.uuid4()
    monkeypatch.setattr(jobs, "enqueue", enqueue)

    async def call():
        return await retention.enqueue_contract_processing(Session(), user_id=uid, generation=2,
                                                           through_id=12, privacy_epoch=4)
    return uid, made, state, call


async def test_dead_right_after_failure_creates_nothing(producer):
    _, made, state, call = producer
    state["terminal"] = dead_row("unexpected_error", ago=timedelta(minutes=5))
    assert await call() is None
    assert not made


async def test_transient_dead_after_interval_creates_one_fresh_redo(producer):
    uid, made, state, call = producer
    state["terminal"] = dead_row("unexpected_error", ago=timedelta(hours=2))
    assert await call() is not None
    job = made[0]
    assert job["dedup_key"] == f"contract-retention-redo:{DEAD}"
    assert job["queue"] == jobs.QUEUE_MEMORY and job["max_attempts"] == 3
    assert job["job_type"] == "contract_compile" and job["user_id"] == uid
    # 죽은 작업의 payload(부분 진행·대체 출력 상태 등)를 복제하지 않고 현재 기준으로 새로 만든다.
    assert job["payload"] == {"retention_enabled": True, "memory_generation": 2,
        "through_message_id": 12, "after_message_id": 7, "privacy_epoch": 4,
        "redo_of": str(DEAD), "redo_depth": 1}
    # 같은 dead를 다시 보면 같은 키라서 두 번째 작업은 생기지 않는다.
    assert await call() is None
    assert len(made) == 2 and made[1]["dedup_key"] == made[0]["dedup_key"]


async def test_content_failure_original_dead_gets_exactly_one_redo(producer):
    _, made, state, call = producer
    state["terminal"] = dead_row("contract_retention_evidence", ago=timedelta(days=1))
    assert await call() is not None
    assert made[0]["payload"]["redo_depth"] == 1 and "no_model" not in made[0]["payload"]
    # 그 재생성 작업이 또 내용 실패로 dead가 되면 모델을 다시 부르는 작업은 만들지 않는다.
    # 모델 없이 커서만 넘기는 마지막 작업 한 건만 만든다.
    redo_dead = uuid.UUID(int=901)
    state["terminal"] = ("dead", redo_dead, NOW - timedelta(days=1), "contract_retention_evidence", "1")
    assert await call() is not None
    assert len(made) == 2 and made[1]["dedup_key"] == f"contract-retention-skip:{redo_dead}"
    assert made[1]["payload"]["no_model"] is True and made[1]["payload"]["redo_depth"] == 2


async def test_redo_lineage_is_recreated_up_to_the_cap_then_gets_one_no_model_skip(producer):
    _, made, state, call = producer
    # 원본 → 1·2·3·4회차 재생성(각 간격 30분·1시간·2시간·4시간이 지난 뒤), 4회차가 또 dead면 상한.
    for step, (depth, wait) in enumerate(((None, timedelta(minutes=30)), (1, timedelta(hours=1)),
                                          (2, timedelta(hours=2)), (3, timedelta(hours=4)))):
        dead = uuid.UUID(int=910 + step)
        raw = None if depth is None else str(depth)
        state["terminal"] = ("dead", dead, NOW - wait + timedelta(minutes=1), "unexpected_error", raw, None)
        assert await call() is None and len(made) == step  # 간격 전에는 만들지 않는다
        state["terminal"] = ("dead", dead, NOW - wait, "unexpected_error", raw, None)
        assert await call() is not None
        assert made[step]["dedup_key"] == f"contract-retention-redo:{dead}"
        assert made[step]["payload"]["redo_depth"] == step + 1
        assert "no_model" not in made[step]["payload"]
    capped = uuid.UUID(int=904)
    state["terminal"] = ("dead", capped, NOW - timedelta(seconds=5), "unexpected_error", "4", None)
    assert await call() is not None
    skip = made[-1]
    assert skip["dedup_key"] == f"contract-retention-skip:{capped}"
    assert skip["queue"] == jobs.QUEUE_MEMORY and skip["max_attempts"] == 3
    assert skip["payload"] == {"retention_enabled": True, "memory_generation": 2,
        "through_message_id": 12, "after_message_id": 7, "privacy_epoch": 4,
        "redo_of": str(capped), "redo_depth": 5, "no_model": True}
    # 같은 dead를 다시 봐도 마지막 작업은 한 건뿐이다(같은 키라 두 번째 등록은 생기지 않는다).
    assert await call() is None
    assert len(made) == 6 and made[5]["dedup_key"] == skip["dedup_key"]
    # 그 마지막 작업이 dead가 되어도 다시 만들지 않는다(반복 없음).
    count = len(made)
    state["terminal"] = ("dead", uuid.UUID(int=905), NOW - timedelta(days=2), "unexpected_error", "5", "true")
    assert await call() is None
    assert len(made) == count


async def test_deterministic_dead_keeps_the_old_stop(producer):
    _, made, state, call = producer
    state["terminal"] = dead_row("contract_background_budget", ago=timedelta(days=3))
    assert await call() is None
    assert not made


async def test_redo_keeps_the_compat_baseline_binding(producer, monkeypatch):
    from tests.test_context_compat_retention_review import baseline
    _, made, state, call = producer
    value = baseline()
    monkeypatch.setattr(retention.context_compat, "load", AsyncMock(return_value=value))
    state["terminal"] = dead_row("timeout", ago=timedelta(hours=1))
    assert await call() is not None
    assert made[0]["payload"]["baseline_binding"] == value.payload()


def test_latest_terminal_keeps_state_first_and_release_scoping():
    sql = str(retention._CONTRACT_LATEST_TERMINAL)
    assert ("SELECT j.state,j.id,j.finished_at,j.last_error_code,j.payload->>'redo_depth',"
            "j.payload->>'no_model'") in sql
    # 다시 만든 작업은 dedup_key가 다르므로 현재 커서의 after_message_id로 찾힌다.
    assert "j.payload->'after_message_id'=to_jsonb(CAST(:cursor AS bigint))" in sql

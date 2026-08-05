"""관계 backfill — 좌표 가드와 런타임 동일 계산.

여기서 지키는 것:
 · 첫 turn이 관계 시작 시각보다 앞서면 **추정으로 메우지 않고 중단**한다
 · 대화 없는 profile도 정상 zero-event `new` state를 갖는다
 · backfill과 런타임이 같은 계산 함수를 쓴다(threshold 재작성 금지)
 · stage는 재계산에서도 내려가지 않는다
"""
from __future__ import annotations

import importlib.util
import pathlib
import uuid
from datetime import date, datetime, timedelta, timezone

# 스크립트라 패키지 import가 안 되므로 파일에서 직접 로드한다.
_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "backfill_relationship_state.py"
_spec = importlib.util.spec_from_file_location("_backfill_rel", _PATH)
bf = importlib.util.module_from_spec(_spec)
# 모듈 하단의 SystemExit(main 실행)을 막고 정의만 가져온다.
_src = _PATH.read_text().split("_env, _rest = split_env_arg")[0]
exec(compile(_src, str(_PATH), "exec"), bf.__dict__)  # noqa: S102

from app.services import relationship as rel  # noqa: E402

UID = uuid.uuid4()
_T0 = datetime(2026, 7, 30, 10, 0, tzinfo=timezone.utc)


class _Conn:
    """turn 행만 돌려주는 최소 스텁. 쓰기는 기록만 한다."""

    def __init__(self, rows):
        self.rows = rows
        self.executed: list[tuple] = []

    async def fetch(self, sql, *args):
        return self.rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _row(turn_seq: int, day: date, occurred: datetime) -> dict:
    return {"turn_seq": turn_seq, "activity_date": day, "occurred_at": occurred}


async def test_first_turn_before_relationship_start_blocks_user():
    """좌표가 어긋나면 '처음 만난 날'이 틀어진다 — 진행하지 않는다."""
    rows = [_row(1, date(2026, 7, 20), _T0 - timedelta(days=10))]
    out = await bf._backfill_user(_Conn(rows), UID, _T0, commit=True)
    assert out["error"] == "first_turn_before_relationship_start"


async def test_blocked_user_writes_nothing():
    rows = [_row(1, date(2026, 7, 20), _T0 - timedelta(days=10))]
    conn = _Conn(rows)
    await bf._backfill_user(conn, UID, _T0, commit=True)
    assert conn.executed == []


async def test_profile_without_turns_gets_zero_event_new_state():
    conn = _Conn([])
    out = await bf._backfill_user(conn, UID, _T0, commit=True)
    assert out == {"turns": 0, "stage": rel.STAGE_NEW, "events": 0}
    assert len(conn.executed) == 1  # state upsert만


async def test_dry_run_writes_nothing():
    rows = [_row(i, date(2026, 8, 1), _T0 + timedelta(hours=i)) for i in range(1, 8)]
    conn = _Conn(rows)
    out = await bf._backfill_user(conn, UID, _T0, commit=False)
    assert conn.executed == []
    assert out["turns"] == 7


async def test_backfill_uses_same_calculation_as_runtime():
    """threshold를 스크립트에 다시 적으면 런타임과 갈라진다 — 같은 함수여야 한다."""
    rows = []
    for d in range(2):
        day = date(2026, 8, 1) + timedelta(days=d)
        for i in range(5):
            rows.append(_row(d * 5 + i, day, _T0 + timedelta(days=d, hours=i)))
    out = await bf._backfill_user(_Conn(rows), UID, _T0, commit=False)
    expected = rel.compute_stage(2, 10)
    assert out["stage"] == expected == rel.STAGE_ACQUAINTED


async def test_daily_cap_applies_in_backfill_too():
    """하루 몰아치기가 backfill에서도 단계를 부풀리지 않는다."""
    day = date(2026, 8, 1)
    rows = [_row(i, day, _T0 + timedelta(minutes=i)) for i in range(100)]
    out = await bf._backfill_user(_Conn(rows), UID, _T0, commit=False)
    assert out["turns"] == 100
    assert out["qualifying"] == rel.MAX_QUALIFYING_TURNS_PER_DAY
    assert out["stage"] == rel.STAGE_NEW  # active_days=1이라 오르지 않는다


async def test_active_day_event_written_once_per_day():
    day = date(2026, 8, 1)
    rows = [_row(i, day, _T0 + timedelta(minutes=i)) for i in range(4)]
    conn = _Conn(rows)
    await bf._backfill_user(conn, UID, _T0, commit=True)
    kinds = [a[1] for sql, a in conn.executed if "relationship_events" in sql]
    assert kinds.count(rel.EVENT_ACTIVE_DAY) == 1
    assert kinds.count(rel.EVENT_NORMAL_TURN) == 4


def test_stage_upsert_is_monotonic_in_sql():
    """재계산이 기존보다 낮아도 내리지 않는다(7.2절) — SQL에 단조 조건이 있어야 한다."""
    assert "array_position" in bf._UPSERT_STATE
    assert "ELSE user_relationship_states.relationship_stage END" in bf._UPSERT_STATE


def test_events_are_idempotent_in_sql():
    assert "ON CONFLICT (user_id, dedup_key) DO NOTHING" in bf._INSERT_EVENT


def test_turnless_rows_are_excluded():
    """turn_seq가 없는 과거 행은 좌표가 없다 — 추정하지 않고 제외한다."""
    assert "turn_seq IS NOT NULL" in bf._TURNS

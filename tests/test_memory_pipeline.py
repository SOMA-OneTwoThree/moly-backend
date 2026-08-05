"""기억 v2 파이프라인 상태 — shadow 진입·커서 전진·관계 event.

여기서 지키는 것:
 · shadow 진입이 historical upper와 collecting을 한 번에 고정한다
 · bootstrap 완료 전에는 live turn ingest를 허용하지 않는다
 · shadow는 기록만 하고 응답에 쓰지 않는다
 · cursor가 뒤로 가지 않고, 다음 turn을 숫자 +1로 가정하지 않는다
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from app.services import memory_pipeline as mp

UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """문장별로 결과를 지정하는 최소 시뮬레이터. 모르는 문장은 통과시키지 않는다."""

    def __init__(self, **plan):
        self.plan = plan
        self.executed: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.executed.append((s, params or {}))
        for key, sql in (
            ("load", mp._LOAD), ("enter", mp._ENTER_SHADOW), ("advance", mp._ADVANCE_SOURCE),
            ("ready", mp._MARK_READY), ("event", mp._ADD_EVENT),
        ):
            if s == str(sql):
                rows = self.plan.get(key, [])
                if key == "event":  # 여러 번 호출되므로 하나씩 소비
                    return _Res([rows.pop(0)] if rows else [])
                return _Res(rows)
        raise AssertionError(f"모르는 문장: {s[:60]}")

    async def scalar(self, stmt, params=None):
        s = str(stmt)
        self.executed.append((s, params or {}))
        if s == str(mp._MAX_TURN):
            return self.plan.get("max_turn", 0)
        if s == str(mp._NEXT_INGEST):
            return self.plan.get("next_ingest")
        raise AssertionError(f"모르는 scalar: {s[:60]}")


# ─────────────────────────────────────────────────────────────
# 1. 상태 해석
# ─────────────────────────────────────────────────────────────
async def test_missing_row_is_legacy_and_does_nothing():
    st = await mp.load(_Session(load=[]), UID)
    assert st.mode == mp.MODE_LEGACY
    assert st.records_v2 is False and st.serves_v2 is False


async def test_shadow_records_but_does_not_serve():
    """shadow의 핵심 — 기록은 하되 응답에 v2를 쓰지 않는다."""
    row = (UID, mp.MODE_SHADOW, mp.BOOTSTRAP_COLLECTING, 10, 0, 0, 10, 0, 1)
    st = await mp.load(_Session(load=[row]), UID)
    assert st.records_v2 is True
    assert st.serves_v2 is False


async def test_v2_mode_serves():
    row = (UID, mp.MODE_V2, mp.BOOTSTRAP_READY, 10, 10, 10, 10, 0, 3)
    st = await mp.load(_Session(load=[row]), UID)
    assert st.records_v2 is True and st.serves_v2 is True


async def test_live_ingest_blocked_until_bootstrap_ready():
    """collecting 동안 live turn을 집어가면 최신 turn이 과거보다 먼저 색인된다."""
    collecting = (UID, mp.MODE_SHADOW, mp.BOOTSTRAP_COLLECTING, 5, 0, 0, 5, 0, 1)
    ready = (UID, mp.MODE_SHADOW, mp.BOOTSTRAP_READY, 5, 0, 0, 5, 0, 2)
    assert (await mp.load(_Session(load=[collecting]), UID)).accepts_live_ingest is False
    assert (await mp.load(_Session(load=[ready]), UID)).accepts_live_ingest is True


# ─────────────────────────────────────────────────────────────
# 2. shadow 진입
# ─────────────────────────────────────────────────────────────
async def test_enter_shadow_pins_historical_upper_and_collecting():
    s = _Session(max_turn=42, enter=[(42,)])
    assert await mp.enter_shadow(s, UID) == 42
    sql, params = next(e for e in s.executed if "INSERT INTO memory_pipeline_states" in e[0])
    assert params["upper"] == 42
    # 같은 문장에서 collecting과 upper를 함께 고정한다(불변식 1)
    assert "'collecting'" in sql and "historical_upper_turn_seq" in sql


async def test_enter_shadow_is_noop_when_already_shadow():
    """재진입이 범위를 다시 흔들면 backfill 경계가 무너진다."""
    s = _Session(max_turn=99, enter=[])
    assert await mp.enter_shadow(s, UID) is None


async def test_enter_shadow_only_upgrades_from_legacy():
    sql = str(mp._ENTER_SHADOW)
    assert "WHERE memory_pipeline_states.mode = 'legacy'" in sql
    # 이미 고정된 upper는 덮어쓰지 않는다
    assert "COALESCE(\n    memory_pipeline_states.historical_upper_turn_seq" in sql


# ─────────────────────────────────────────────────────────────
# 3. 커서
# ─────────────────────────────────────────────────────────────
async def test_source_cursor_never_moves_backwards():
    assert "GREATEST(source_through_turn_seq, :turn_seq)" in str(mp._ADVANCE_SOURCE)


async def test_advance_source_skips_legacy_users():
    assert "mode <> 'legacy'" in str(mp._ADVANCE_SOURCE)
    assert await mp.advance_source(_Session(advance=[]), UID, turn_seq=5) is None


async def test_next_turn_is_looked_up_not_incremented():
    """turn_seq는 연속이 아닐 수 있다 — +1을 가정하면 gap에서 영원히 멈춘다."""
    s = _Session(next_ingest=17)
    assert await mp.next_ingest_turn(s, UID, cursor=9) == 17
    assert "MIN(m.turn_seq)" in str(mp._NEXT_INGEST)


def test_next_turn_reads_messages_not_legacy_watermark_table():
    """legacy memory_source_turns에는 turn_seq 컬럼이 없다 — 거기서 조회하면 런타임에 깨진다."""
    sql = str(mp._NEXT_INGEST)
    assert "FROM messages m" in sql
    assert "memory_source_turns" not in sql
    # 고정한 historical/source 범위를 넘어선 turn은 제외한다.
    assert "m.turn_seq <= s.source_through_turn_seq" in sql


async def test_next_turn_none_when_caught_up():
    assert await mp.next_ingest_turn(_Session(next_ingest=None), UID, cursor=9) is None


async def test_mark_ready_only_from_collecting():
    assert "bootstrap_status='collecting'" in str(mp._MARK_READY)
    assert await mp.mark_bootstrap_ready(_Session(ready=[]), UID) is False
    assert await mp.mark_bootstrap_ready(_Session(ready=[(10,)]), UID) is True


# ─────────────────────────────────────────────────────────────
# 4. 관계 event
# ─────────────────────────────────────────────────────────────
async def test_first_turn_of_day_records_both_events():
    s = _Session(event=[(1,), (2,)])
    n = await mp.record_turn_events(
        s, UID, turn_seq=3, activity_date=date(2026, 8, 5), occurred_at=_T0
    )
    assert n == 2
    kinds = [p["event_type"] for sql, p in s.executed if "relationship_events" in sql]
    assert kinds == ["normal_turn_committed", "active_day_started"]


async def test_repeat_turn_of_day_records_only_turn_event():
    """같은 날 두 번째 turn — active_day는 ON CONFLICT로 무시된다."""
    s = _Session(event=[(1,), None])
    n = await mp.record_turn_events(
        s, UID, turn_seq=4, activity_date=date(2026, 8, 5), occurred_at=_T0
    )
    assert n == 1


async def test_events_are_deduped_at_db_level():
    assert "ON CONFLICT (user_id, dedup_key) DO NOTHING" in str(mp._ADD_EVENT)


# ─────────────────────────────────────────────────────────────
# 5. chat Phase B 배선 — legacy는 건드리지 않고 shadow만 기록한다
# ─────────────────────────────────────────────────────────────
async def test_chat_phase_b_is_noop_for_legacy_users(monkeypatch):
    from app.services import chat

    calls: list[str] = []
    monkeypatch.setattr(mp, "load", lambda s, u: _legacy_state(u))
    monkeypatch.setattr(mp, "advance_source", _record("advance", calls))
    monkeypatch.setattr(mp, "record_turn_events", _record("events", calls))
    await chat._record_memory_v2(
        None, UID, turn_seq=1, activity_date=date(2026, 8, 5), now=_T0
    )
    assert calls == []  # legacy 사용자에겐 v2 흔적을 남기지 않는다


async def test_chat_phase_b_records_for_shadow_users(monkeypatch):
    from app.services import chat

    calls: list[str] = []
    monkeypatch.setattr(mp, "load", lambda s, u: _shadow_state(u))
    monkeypatch.setattr(mp, "advance_source", _record("advance", calls))
    monkeypatch.setattr(mp, "record_turn_events", _record("events", calls))
    await chat._record_memory_v2(
        None, UID, turn_seq=7, activity_date=date(2026, 8, 5), now=_T0
    )
    assert calls == ["advance", "events"]


def _record(name: str, sink: list[str]):
    async def _fn(*a, **kw):
        sink.append(name)
        return None

    return _fn


async def _legacy_state(user_id):
    return mp.PipelineState(
        user_id=user_id, mode=mp.MODE_LEGACY, bootstrap_status=mp.BOOTSTRAP_LEGACY,
        source_through_turn_seq=0, ingest_through_turn_seq=0,
        consolidated_through_turn_seq=0, historical_upper_turn_seq=None,
        privacy_epoch=0, revision=0,
    )


async def _shadow_state(user_id):
    return mp.PipelineState(
        user_id=user_id, mode=mp.MODE_SHADOW, bootstrap_status=mp.BOOTSTRAP_COLLECTING,
        source_through_turn_seq=6, ingest_through_turn_seq=0,
        consolidated_through_turn_seq=0, historical_upper_turn_seq=6,
        privacy_epoch=0, revision=1,
    )


# ─────────────────────────────────────────────────────────────
# 6. ingest/consolidation 커서 — 서로를 앞지르지 않는다
# ─────────────────────────────────────────────────────────────
def test_ingest_cursor_cannot_outrun_source():
    """source보다 앞서면 아직 안 만들어진 turn을 처리했다는 뜻이다."""
    sql = str(mp._ADVANCE_INGEST)
    assert ":turn_seq <= source_through_turn_seq" in sql
    assert "GREATEST(ingest_through_turn_seq" in sql


def test_consolidated_cursor_cannot_outrun_ingest():
    """판정은 색인된 것에 대해서만 한다."""
    sql = str(mp._ADVANCE_CONSOLIDATED)
    assert ":turn_seq <= ingest_through_turn_seq" in sql
    assert "GREATEST(consolidated_through_turn_seq" in sql


def test_cursor_advances_skip_legacy_users():
    for sql in (mp._ADVANCE_INGEST, mp._ADVANCE_CONSOLIDATED):
        assert "mode <> 'legacy'" in str(sql)


# ─────────────────────────────────────────────────────────────
# 7. enqueue — 사용자당 한 번에 한 turn만 흐른다
# ─────────────────────────────────────────────────────────────
def test_dedup_keys_are_deterministic_and_kind_separated():
    """같은 turn을 두 번 enqueue해도 한 행. ingest와 consolidate는 키 공간이 다르다."""
    a = mp.ingest_dedup_key(UID, 7)
    assert a == mp.ingest_dedup_key(UID, 7)
    assert a != mp.ingest_dedup_key(UID, 8)
    assert a != mp.consolidate_dedup_key(UID, 7)


async def test_next_ingest_enqueues_actual_next_turn_not_plus_one():
    """turn_seq는 연속이 아닐 수 있다 — 실제 다음 turn을 조회해 건다."""
    calls = {}

    class _S(_Session):
        pass

    s = _S(next_ingest=23)
    import app.services.jobs as jobs_mod

    async def _fake_enqueue(session, **kw):
        calls.update(kw)
        return uuid.uuid4()

    original = jobs_mod.enqueue
    jobs_mod.enqueue = _fake_enqueue
    try:
        got = await mp.enqueue_next_ingest(s, UID, cursor=9)
    finally:
        jobs_mod.enqueue = original
    assert got == 23
    assert calls["dedup_key"] == mp.ingest_dedup_key(UID, 23)
    assert calls["payload"]["turn_seq"] == 23


async def test_next_ingest_returns_none_when_caught_up():
    """따라잡았으면 잡을 만들지 않는다 — 빈 잡이 큐를 돌지 않게."""
    assert await mp.enqueue_next_ingest(_Session(next_ingest=None), UID, cursor=9) is None


def test_chat_enqueues_only_when_bootstrap_ready_and_caught_up():
    """collecting 중이거나 이미 밀린 turn이 있으면 새로 걸지 않는다."""
    import inspect

    from app.services import chat

    src = inspect.getsource(chat._record_memory_v2)
    assert "state.accepts_live_ingest" in src
    assert "ingest_through_turn_seq >= state.source_through_turn_seq" in src


def test_ingest_success_enqueues_followups_inside_apply_domain():
    """후속 잡은 fenced transaction 안에서만 생성된다 — lease 잃은 소비자가 흘리지 않게."""
    import inspect

    from worker import mem0_jobs

    src = inspect.getsource(mem0_jobs.handle_mem0_ingest)
    advance_block = src.split("async def _advance")[1]
    assert "enqueue_consolidate" in advance_block
    assert "enqueue_next_ingest" in advance_block

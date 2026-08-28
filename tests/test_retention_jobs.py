"""Phase 5 retention 잡 5종 — 절대 조건(로드맵 v4)의 SQL 계약 고정.

이 테스트가 지키는 것(하나라도 풀리면 데이터 사고):
 · 5-2: started·unknown_usage는 절대 삭제되지 않는다. 롤업은 단일 문장 가산형이다.
 · 5-3: dead·replay 사슬은 절대 지우지 않는다.
 · 5-4: planned 비대상 + pending registry가 참조하는 후보는 NOT EXISTS로 제외(잠복 결함 §9.6).
 · 5-5: processed만, 365일. failed/pending 무기한.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from worker import retention_jobs as rt


def _sql(stmt) -> str:
    return " ".join(str(stmt).split())


# ── 5-1 ──────────────────────────────────────────────────────
def test_idempotency_gc_only_expired_dedupe():
    sql = _sql(rt._IDEMPOTENCY_GC)
    assert "dedupe_expires_at IS NOT NULL AND dedupe_expires_at <= now()" in sql
    assert "LIMIT :n" in sql and "SKIP LOCKED" in sql  # bounded + 경합 안전


# ── 5-2 ──────────────────────────────────────────────────────
def test_rollup_never_touches_started_or_unknown():
    """started 삭제 = 진행 중 호출 증발, unknown_usage 삭제 = 미확정 비용의 0원 확정(불변식 2)."""
    sql = _sql(rt._USAGE_ROLLUP)
    assert "status IN ('completed','failed')" in sql
    assert "'started'" not in sql and "'unknown_usage'" not in sql


def test_rollup_is_single_statement_additive():
    """DELETE…RETURNING→INSERT ON CONFLICT 가산이 한 문장 — 어느 지점에서 죽어도 정확히 1회 집계."""
    sql = _sql(rt._USAGE_ROLLUP)
    assert sql.count("DELETE FROM ai_usage_ledger") == 1
    assert "RETURNING" in sql and "ON CONFLICT" in sql
    assert "calls = r.calls + EXCLUDED.calls" in sql  # 가산형(덮어쓰기 금지)


def test_rollup_axis_is_kst_date_not_activity_date():
    """activity_date는 74% NULL(§9.6) — 축은 KST date여야 한다."""
    sql = _sql(rt._USAGE_ROLLUP)
    assert "activity_date" not in sql
    assert "AT TIME ZONE 'Asia/Seoul'" in sql
    # 삭제 술어는 sargable(started_at < 상수) — 행마다 캐스트하면 인덱스 불가
    assert "WHERE status IN ('completed','failed') AND started_at <" in sql


# ── 5-3 ──────────────────────────────────────────────────────
def test_jobs_gc_preserves_dead_and_replay_chains():
    sql = _sql(rt._JOBS_GC)
    assert "state IN ('succeeded','cancelled')" in sql  # dead 비대상
    assert "replay_of IS NULL" in sql  # replay 사슬의 자식 비대상
    assert "NOT EXISTS (SELECT 1 FROM async_jobs r WHERE r.replay_of = j.id)" in sql  # 참조되는 원본 비대상
    assert "interval '14 days'" in sql


def test_orphan_mutex_cleanup_uses_expiry_predicates():
    """살아 있는 클레임(30분)·lease(초 단위)와 7일 차이 — 만료 기준 술어 명시(4차 검증)."""
    assert "claimed_at < now() - interval '7 days'" in _sql(rt._ORPHAN_DIARY_CLAIMS)
    assert "lease_until < now() - interval '7 days'" in _sql(rt._ORPHAN_ACTIVE_TURNS)


# ── 5-4 ──────────────────────────────────────────────────────
def test_candidate_gc_never_touches_planned():
    for stmt in (rt._CANDIDATE_GC_COMMITTED, rt._CANDIDATE_GC_DEAD):
        assert "'planned'" not in _sql(stmt)


def test_candidate_gc_excludes_pending_registry_references():
    """생략 불가(§9.6 잠복 결함): 커서를 지나친 pending의 재판정이 후보 본문을 읽는다 —
    지우면 candidate_text_missing → dead 무한 루프 + 그 기억 영구 pending."""
    for stmt in (rt._CANDIDATE_GC_COMMITTED, rt._CANDIDATE_GC_DEAD):
        sql = _sql(stmt)
        assert "NOT EXISTS" in sql and "semantic_status = 'pending'" in sql


def test_candidate_gc_cursor_lives_in_pipeline_states():
    """consolidated_through_turn_seq는 candidates가 아니라 memory_pipeline_states 소속(§9.6)."""
    sql = _sql(rt._CANDIDATE_GC_COMMITTED)
    assert "JOIN memory_pipeline_states s ON s.user_id = c.user_id" in sql
    assert "c.turn_seq <= s.consolidated_through_turn_seq" in sql


# ── 5-5 ──────────────────────────────────────────────────────
def test_rc_events_gc_only_processed_365d():
    sql = _sql(rt._RC_EVENTS_GC)
    assert "status='processed'" in sql and "interval '365 days'" in sql
    assert "'failed'" not in sql and "'pending'" not in sql  # 결제 감사 무기한 보존


# ── 성공 기록(관측 단일 소스) ────────────────────────────────
class _NoopSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, *a, **k):
        return SimpleNamespace(rowcount=0)

    async def commit(self):
        pass

    async def scalar(self, *a, **k):
        return 0


async def test_every_handler_records_last_success(monkeypatch):
    """핸들러 5종 전부 성공 시 app_config에 last_success를 기록해야 한다 —
    async_jobs 이력은 5-3의 14일 GC로 소멸하므로 /health/deep 판정의 유일한 소스다."""
    recorded: list[str] = []

    async def _set(session, key, value):
        recorded.append(key)

    monkeypatch.setattr(rt.config_store, "set_config_value", _set)
    monkeypatch.setattr(rt, "get_sessionmaker", lambda: _NoopSession)

    job = SimpleNamespace(payload={})
    for handler in (rt.handle_retention_idempotency, rt.handle_usage_rollup,
                    rt.handle_retention_jobs, rt.handle_candidate_gc, rt.handle_rc_events_gc):
        await handler(job)
    prefix = rt.config_store.RETENTION_LAST_SUCCESS_PREFIX
    assert recorded == [
        prefix + rt.JOB_RETENTION_IDEMPOTENCY, prefix + rt.JOB_USAGE_ROLLUP,
        prefix + rt.JOB_RETENTION_JOBS, prefix + rt.JOB_MEM0_CANDIDATE_GC,
        prefix + rt.JOB_RETENTION_RC_EVENTS,
    ]


# ── tick enqueue ─────────────────────────────────────────────
class _EnqSession:
    pass


async def test_enqueue_daily_gated_to_kst_after_5(monkeypatch):
    """KST hour<5면 아무것도 안 건다. hour>=5면 그날 dedup 키로 하루 1회 수렴(self-heal)."""
    calls: list[dict] = []

    async def _enq(session, **kw):
        calls.append(kw)
        return uuid.uuid4()

    monkeypatch.setattr(rt.jobs, "enqueue", _enq)

    kst = ZoneInfo("Asia/Seoul")
    # KST 03:00 — 게이트 이전
    made = await rt.enqueue_daily(_EnqSession(), datetime(2026, 8, 29, 3, 0, tzinfo=kst))
    assert made == 0 and calls == []

    # KST 14:00 — 05시 워커가 죽어 있었어도 그날 중 아무 틱에서 self-heal
    made = await rt.enqueue_daily(_EnqSession(), datetime(2026, 8, 29, 14, 0, tzinfo=kst))
    assert made == 4  # 일간 4종(월간 rc는 1일에만)
    assert {c["job_type"] for c in calls} == set(rt.DAILY_JOB_TYPES)
    assert all(c["dedup_key"].endswith(":2026-08-29") or ":2026-08-29:" in c["dedup_key"]
               or c["dedup_key"] == f"{c['job_type']}:2026-08-29" for c in calls)
    assert all(c["priority"] == rt.RETENTION_PRIORITY for c in calls)  # 후순위 — privclean 우선

    # 매월 1일엔 rc 이벤트 GC 동반(월 dedup 키)
    calls.clear()
    made = await rt.enqueue_daily(_EnqSession(), datetime(2026, 9, 1, 6, 0, tzinfo=kst))
    assert made == 5
    rc = [c for c in calls if c["job_type"] == rt.JOB_RETENTION_RC_EVENTS]
    assert rc and rc[0]["dedup_key"] == "retention_rc_events:2026-09"


async def test_continuation_chains_with_seq(monkeypatch):
    """잔량이 남으면 같은 날짜 사슬의 seq+1 잡을 건다(privclean 패턴)."""
    calls: list[dict] = []

    async def _enq(session, **kw):
        calls.append(kw)
        return uuid.uuid4()

    monkeypatch.setattr(rt.jobs, "enqueue", _enq)
    job = SimpleNamespace(payload={"seq": 2, "date_key": "2026-08-29"})
    await rt._continuation(job, rt.JOB_RETENTION_JOBS)(_EnqSession())
    assert calls[0]["dedup_key"] == "retention_jobs_gc:2026-08-29:3"
    assert calls[0]["payload"]["seq"] == 3

"""헬스·모니터링 엔드포인트.

- GET /health          liveness(공개) — 프로세스 생존 + 배포 버전.
- GET /health/ready    readiness(공개) — DB 도달성. 외부 상시감시(Betterstack)의 유일 대상. 503 on down.
- GET /health/deep     진단(헤더인증·수동/배포직후 전용) — 기록된 상태 종합(LLM 호출 없음). 외부 상시폴링 금지.
- GET /health/synthetic 합성(헤더인증·스케줄) — 의존성(DB·LLM) 능동 점검. 유저/통계 미오염.
- GET /health/queues  잡 큐(헤더인증) — 큐별 ready/running/dead + oldest dead age. 이관 게이트.

deep·synthetic 인증 = 헤더 X-Health-Token 상수시간 비교. 토큰 설정 시 항상 요구,
미설정 시 비-local은 403(fail-closed)·local은 통과(개발 편의).
"""
from __future__ import annotations

import hmac
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.core.db import get_session
from app.models.user_daily_stats import UserDailyStats
from app.schemas.common import HealthResponse
from app.services import config_store, jobs, llm, slack_notify  # noqa: F401 (slack_notify: 향후 확장)

router = APIRouter(tags=["system"])

_KST = ZoneInfo("Asia/Seoul")
_WORKER_STALE_SEC = 2 * 3600  # 워커 마지막 성공이 이보다 오래면 stale(15분 틱 기준 8회 연속 누락)


def require_health_token(
    x_health_token: str | None = Header(default=None, alias="X-Health-Token"),
) -> None:
    """deep·synthetic 인증. 토큰 설정 시 상수시간 일치 요구, 미설정 시 비-local 403(fail-closed)."""
    expected = settings.health_token
    if expected:
        if not x_health_token or not hmac.compare_digest(x_health_token, expected):
            raise errors.unauthorized("헬스 토큰이 올바르지 않아요.")
    elif settings.environment != "local":
        raise errors.forbidden("헬스 토큰이 설정되지 않았어요.")


@router.get("/health", response_model=HealthResponse)
def health_check() -> dict[str, str]:
    """liveness — 인증 불필요(로드밸런서/배포 프로브). 버전으로 배포 반영 확인."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "env": settings.environment,
        "version": settings.git_sha,
    }


@router.get("/health/ready", include_in_schema=False)
async def health_ready(
    response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """readiness — DB 도달성. 실패 시 503(외부 모니터가 상태코드로 판정). 공개."""
    try:
        await session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001  # 어떤 DB 오류든 down으로 신호
        response.status_code = 503
        return {"status": "down", "db": "down", "version": settings.git_sha}
    return {"status": "ok", "db": "ok", "version": settings.git_sha}


@router.get("/health/deep", dependencies=[Depends(require_health_token)], include_in_schema=False)
async def health_deep(
    response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """진단 — 기록된 상태 종합(LLM 호출 없음). 외부 상시폴링 금지, 수동/배포직후 전용."""
    response.headers["Cache-Control"] = "no-store"
    now = datetime.now(timezone.utc)
    out: dict[str, Any] = {"version": settings.git_sha}
    degraded = False  # 주입 Response.status_code 기본값이 200이 아니므로 로컬 플래그로 판정

    # DB
    try:
        await session.execute(text("SELECT 1"))
        out["db"] = "ok"
    except Exception:  # noqa: BLE001
        out["db"] = "down"
        degraded = True

    # 워커 마지막 성공(app_config 기록) → stale 판정
    worker: dict[str, Any] = {"last_success": None, "stale": True, "age_sec": None}
    try:
        vals = await config_store.get_config_values(session, [config_store.WORKER_LAST_SUCCESS_KEY])
        raw = vals.get(config_store.WORKER_LAST_SUCCESS_KEY)
        if isinstance(raw, str):
            last = datetime.fromisoformat(raw)
            age = (now - last).total_seconds()
            worker = {"last_success": raw, "stale": age > _WORKER_STALE_SEC, "age_sec": int(age)}
    except Exception:  # noqa: BLE001  (기록 파싱 실패는 stale로 둔다)
        pass
    out["worker"] = worker
    if worker["stale"]:
        degraded = True

    # 오늘(KST) 누적 billable·활성 유저 — user_daily_stats(작은 테이블) 합산
    try:
        today = datetime.now(_KST).date()
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(UserDailyStats.tokens_used), 0),
                    func.count(UserDailyStats.user_id),
                ).where(UserDailyStats.activity_date == today)
            )
        ).one()
        out["today"] = {"billable": int(row[0]), "active_users": int(row[1])}
    except Exception:  # noqa: BLE001
        out["today"] = {"billable": None, "active_users": None}

    # Phase 5-6: 증식 관측 — 주요 테이블 크기·vecs 팽창비, retention 잡 최근 성공.
    # retention stale 임계는 **25h**다(잡 주기 24h — 24h 임계면 정상 상태가 상시 오탐).
    try:
        srow = (
            await session.execute(text("""
                SELECT pg_total_relation_size('public.async_jobs'),
                       pg_total_relation_size('public.idempotency_keys'),
                       pg_total_relation_size('public.ai_usage_ledger'),
                       pg_total_relation_size('vecs.moly_memories_v2'),
                       (SELECT COALESCE(reltuples,0) FROM pg_class WHERE oid='vecs.moly_memories_v2'::regclass)
            """))
        ).one()
        out["tables"] = {
            "async_jobs_bytes": int(srow[0]), "idempotency_keys_bytes": int(srow[1]),
            "ai_usage_ledger_bytes": int(srow[2]), "vecs_memories_bytes": int(srow[3]),
            # 팽창비: 행당 바이트(대략) — repack/autovacuum 평형 감시용 조잡 신호.
            # ANALYZE 전 reltuples=-1 — >0 가드 없이는 음수가 노출된다.
            "vecs_bytes_per_row": int(srow[3] / srow[4]) if srow[4] > 0 else None,
        }
    except Exception:  # noqa: BLE001
        out["tables"] = None

    # retention 성공 시각은 **app_config 기록**으로 판정한다(핸들러가 성공 시 기록 — 단일 소스).
    # async_jobs 이력 기반은 불가: 5-3이 succeeded 잡을 14일에 지우므로 월간 rc 잡의 증거가
    # 매달 중순에 소멸해 후반 내내 상시 503이 된다(Phase 5 교차검증 [중-1]).
    try:
        # 일간 4종 임계 25h(24h면 상시 오탐), 월간 rc 이벤트 GC는 32일.
        thresholds = (('retention_idempotency_gc', 25), ('usage_ledger_rollup', 25),
                      ('retention_jobs_gc', 25), ('mem0_candidate_gc', 25),
                      ('retention_rc_events', 32 * 24))
        prefix = config_store.RETENTION_LAST_SUCCESS_PREFIX
        vals = await config_store.get_config_values(session, [prefix + jt for jt, _ in thresholds])
        retention = {}
        stale_retention = False
        for jt, stale_h in thresholds:
            raw = vals.get(prefix + jt)
            last = None
            if isinstance(raw, str):
                try:
                    last = datetime.fromisoformat(raw)
                except ValueError:
                    pass
            age = (now - last).total_seconds() if last else None
            retention[jt] = {"last_success": raw if last else None,
                             "age_sec": int(age) if age is not None else None}
            # 첫 실행 전(None)은 stale 아님 — 월간 잡은 배포 후 다음달 1일까지 기록이 없는 게
            # 정상이다. 한 번도 안 도는 고장은 기존 dead→Slack·/health/queues가 잡는다.
            if age is not None and age > stale_h * 3600:
                stale_retention = True
        out["retention"] = {"jobs": retention, "stale": stale_retention}
        if stale_retention:
            degraded = True
    except Exception:  # noqa: BLE001
        out["retention"] = None

    if degraded:
        response.status_code = 503
    out["status"] = "degraded" if degraded else "ok"
    return out


# 기억 사슬 정지 신호 네 가지.
#  · behind        커서가 뒤처진 사용자 수(정상 처리 중인 사람도 잠깐 여기 들어온다)
#  · stalled       그중 **대기·실행 중인 잡이 하나도 없는** 사람 — 이게 진짜 정지 신호다
#  · unjudged      판정 못 받은 채 오래 남은 기억 수 — 회상에 안 나오고 전환 관문도 막는다
#  · collecting    진입 절차가 중간에 끊겨 갇힌 사용자 수
_MEMORY_STALL = text("""
SELECT
  (SELECT count(*) FROM memory_pipeline_states
    WHERE mode <> 'legacy' AND ingest_through_turn_seq < source_through_turn_seq) AS behind,
  (SELECT count(*) FROM memory_pipeline_states s
    WHERE s.mode <> 'legacy' AND s.bootstrap_status = 'ready'
      AND s.ingest_through_turn_seq < s.source_through_turn_seq
      -- ⚠️ 기다린 시간은 **처리 안 된 턴의 나이**로 잰다. 상태 행의 `updated_at`은 대화할
      --    때마다 새로 찍혀서, 멈춘 사람이 계속 대화하면 영원히 안 걸린다.
      AND EXISTS (SELECT 1 FROM messages m
                   WHERE m.user_id = s.user_id AND m.kind = 'normal'
                     AND m.turn_seq > s.ingest_through_turn_seq
                     AND m.turn_seq <= s.source_through_turn_seq
                     AND m.created_at < now() - interval '30 minutes')
      AND NOT EXISTS (SELECT 1 FROM async_jobs j
                       WHERE j.user_id = s.user_id AND j.job_type = 'mem0_ingest'
                         AND j.state IN ('ready','running'))) AS stalled,
  (SELECT count(*) FROM mem0_memory_registry
    WHERE semantic_status = 'pending' AND created_at < now() - interval '30 minutes') AS unjudged,
  (SELECT count(*) FROM memory_pipeline_states
    WHERE bootstrap_status = 'collecting'
      AND updated_at < now() - interval '2 hours') AS collecting
""")


@router.get("/health/queues", dependencies=[Depends(require_health_token)], include_in_schema=False)
async def health_queues(
    response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """잡 큐(async_jobs) 상태 — 큐별 ready/running/dead count + oldest dead age(초).

    기존 전역 틱의 핸들러를 큐로 옮기는 **이관 게이트** 지표다. dead는 자동 삭제하지 않으므로
    dead_total 증가 또는 미확인 1건 이상은 배포 gate 실패로 취급한다(운영 규칙).
    DB 도달 실패만 503 — dead가 있다는 사실 자체는 이 엔드포인트의 실패가 아니다(데이터로 노출).
    """
    response.headers["Cache-Control"] = "no-store"
    try:
        queues = await jobs.queue_stats(session)
    except Exception:  # noqa: BLE001
        response.status_code = 503
        return {"status": "down", "version": settings.git_sha}
    # 기억 사슬이 멈춘 신호. **이 숫자가 없으면 정지가 침묵으로만 나타난다** — 지금까지
    # 발견된 정지 사고가 전부 사람이 우연히 알아챈 것이었다(2026-08-08 감사).
    try:
        memory = dict((await session.execute(_MEMORY_STALL)).mappings().first() or {})
    except Exception:  # noqa: BLE001
        memory = {}
    return {
        "status": "ok",
        "version": settings.git_sha,
        "queues": queues,
        "dead_total": sum(q["dead"] for q in queues.values()),
        "memory": memory,
    }


@router.get("/health/synthetic", dependencies=[Depends(require_health_token)], include_in_schema=False)
async def health_synthetic(
    response: Response, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """합성 — 의존성(DB·LLM) 능동 점검. 실제 유저·통계·일기 미오염(유저 자체가 없음).

    LLM은 성공(예외 없음)=up으로 본다(GPT-5 계열이 reasoning으로 토큰 소진해 빈 텍스트여도 도달은 정상).
    하나라도 down이면 503.
    """
    response.headers["Cache-Control"] = "no-store"
    out: dict[str, Any] = {"version": settings.git_sha}
    ok = True

    t0 = time.monotonic()
    try:
        await session.execute(text("SELECT 1"))
        out["db"] = {"status": "ok", "latency_ms": int((time.monotonic() - t0) * 1000)}
    except Exception:  # noqa: BLE001
        out["db"] = {"status": "down"}
        ok = False

    if settings.synthetic_check_llm:
        t1 = time.monotonic()
        try:
            res = await llm.generate(
                ["헬스 점검용. 짧게 답해."],
                [{"role": "user", "content": "ping"}],
                max_tokens=32,
            )
            out["llm"] = {
                "status": "ok",
                "latency_ms": int((time.monotonic() - t1) * 1000),
                "empty": not (res.text or "").strip(),
            }
        except Exception as e:  # noqa: BLE001  # 도달 실패만 down(예외)
            out["llm"] = {"status": "down", "error": type(e).__name__}
            ok = False
    else:
        out["llm"] = {"status": "skipped"}

    if not ok:
        response.status_code = 503
    out["status"] = "ok" if ok else "down"
    return out

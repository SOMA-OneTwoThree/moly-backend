"""헬스·모니터링 엔드포인트.

- GET /health          liveness(공개) — 프로세스 생존 + 배포 버전.
- GET /health/ready    readiness(공개) — DB 도달성. 외부 상시감시(Betterstack)의 유일 대상. 503 on down.
- GET /health/deep     진단(헤더인증·수동/배포직후 전용) — 기록된 상태 종합(LLM 호출 없음). 외부 상시폴링 금지.
- GET /health/synthetic 합성(헤더인증·스케줄) — 의존성(DB·LLM) 능동 점검. 유저/통계 미오염.

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
from app.services import config_store, llm, slack_notify  # noqa: F401 (slack_notify: 향후 확장)

router = APIRouter(tags=["system"])

_KST = ZoneInfo("Asia/Seoul")
_WORKER_LAST_SUCCESS_KEY = "monitoring:worker_last_success"
_WORKER_STALE_SEC = 2 * 3600  # 워커 마지막 성공이 이보다 오래면 stale(매시 틱이라 2h면 여러 틱 누락)


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
        vals = await config_store.get_config_values(session, [_WORKER_LAST_SUCCESS_KEY])
        raw = vals.get(_WORKER_LAST_SUCCESS_KEY)
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

    if degraded:
        response.status_code = 503
    out["status"] = "degraded" if degraded else "ok"
    return out


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

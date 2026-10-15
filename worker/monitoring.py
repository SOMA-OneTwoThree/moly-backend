"""워커 관측과 일일 점검. 사용자/이벤트 원문 없이 집계만 다룬다."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.config import settings
from app.core.db import get_sessionmaker
from app.services import monitoring_state, config_store, jobs

_log = logging.getLogger(__name__)
_KST = ZoneInfo("Asia/Seoul")
_MONITORING_BUDGET_S = 15.0
_RC_SQL = text("""
WITH unresolved AS (
 SELECT event_id, status, received_at,
   (status='failed' OR received_at IS NULL OR received_at < :stale_before) AS actionable,
   (last_error = 'held:provider_history_unavailable'
    OR last_error LIKE 'RevenueCatError(''RevenueCat read %'
    OR last_error LIKE 'RevenueCatError(''RevenueCat invalid list%'
    OR last_error LIKE 'RevenueCatError(''RevenueCat list incomplete%') AS provider_unavailable,
   CASE WHEN upper(payload->>'environment') IN ('PRODUCTION','SANDBOX')
        THEN upper(payload->>'environment') ELSE 'UNKNOWN' END AS environment
 FROM revenuecat_events
 WHERE status IN ('failed','pending')
)
SELECT environment, count(*) FILTER (WHERE status='failed') AS failed,
       count(*) FILTER (WHERE status='pending' AND actionable) AS pending,
       min(received_at) FILTER (WHERE actionable) AS oldest,
       coalesce(md5(string_agg(event_id || ':' || status, ',' ORDER BY event_id)
         FILTER (WHERE actionable)), 'empty') AS fingerprint,
       count(*) FILTER (WHERE provider_unavailable) AS provider_unavailable,
       coalesce(md5(string_agg(event_id || ':' || status, ',' ORDER BY event_id)
         FILTER (WHERE provider_unavailable)), 'empty') AS provider_fingerprint
FROM unresolved GROUP BY environment
""")
_COST_SQL = text("""
SELECT count(*) AS calls,
       count(*) FILTER (WHERE cost_micro_usd IS NOT NULL AND status='completed') AS priced,
       sum(cost_micro_usd) FILTER (WHERE status='completed') AS cost,
       count(*) FILTER (WHERE cost_micro_usd IS NULL OR status <> 'completed') AS unknown,
       count(*) FILTER (WHERE cache_write_estimated) AS cache_estimated,
       count(*) FILTER (WHERE experiment_id IS NOT NULL) AS experiments
FROM ai_usage_ledger WHERE started_at >= :start AND started_at < :end
""")


def cost_window(now: datetime) -> tuple[datetime, datetime]:
    end = datetime.combine(now.astimezone(_KST).date(), time(), _KST)
    return (end - timedelta(days=1)).astimezone(timezone.utc), end.astimezone(timezone.utc)


async def rc_snapshot(session, now: datetime) -> dict:
    result = await session.execute(_RC_SQL, {"stale_before": now - timedelta(minutes=60)})
    out = {env: {"failed": 0, "pending": 0, "oldest": None, "fingerprint": "empty",
                 "provider_unavailable": 0, "provider_fingerprint": "empty"}
           for env in ("PRODUCTION", "SANDBOX", "UNKNOWN")}
    for row in result.mappings():
        out[row["environment"]] = dict(row)
    return out


def rc_line(env: str, data: dict, now: datetime) -> str:
    oldest = data.get("oldest")
    age = f"{max(0, int((now - oldest).total_seconds() / 3600))}시간" if oldest else "미확인"
    return (f"RC {env}: failed {data['failed']}건 / 장기 pending {data['pending']}건"
            + (f" / 최장 {age}" if data["failed"] + data["pending"] else "")
            + f" / 외부 이력 확인 불가 {data.get('provider_unavailable', 0)}건")


def cost_line(row: dict, now: datetime) -> str:
    day = (now.astimezone(_KST) - timedelta(days=1)).date()
    prefix = f"AI 원장 ({day} KST 00–24시 시작 호출): "
    if not row["calls"]:
        return prefix + "계측 기록 0건 (전체 비용 0을 뜻하지 않음)"
    cost = f"${row['cost'] / 1_000_000:.4f}" if row["priced"] else "확정 가능 계측 없음"
    return (prefix + f"계측 추정 {cost} / 비용 미확정 {row['unknown']}건"
            f" / cache 추정 {row['cache_estimated']}건 / 실험 {row['experiments']}건 포함"
            " (미계측 경로 제외, 청구 확정액 아님)")


async def _daily_message(now: datetime, rc: dict | None, counts: dict | None = None) -> str:
    lines = []
    attention = False
    unavailable = False
    previous_signals = {"worker_diary": "일기", "worker_memory": "기억", "worker_timeout": "시간 초과",
                        "rc_processor": "RC 처리기", "worker_push": "푸시"}
    try:
        async with get_sessionmaker()() as session:
            values = await config_store.get_config_values(session, [config_store.WORKER_LAST_SUCCESS_KEY,
                *(monitoring_state.key_for(signal) for signal in previous_signals)])
        value = values.get(config_store.WORKER_LAST_SUCCESS_KEY)
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else None
        age = max(0, int((now - stamp).total_seconds() / 60)) if stamp else None
        lines.append(f"마지막 tick 완료: {age}분 전 (개별 작업 성공 보증 아님)"
                     if age is not None else "마지막 tick 완료: 관측 불가")
        unavailable |= age is None
        attention |= age is not None and age > 120  # 기존 /health/deep의 2시간 stale 기준.
        pending = []
        for signal, label in previous_signals.items():
            state = values.get(monitoring_state.key_for(signal))
            if isinstance(state, dict) and state.get("active"):
                pending.append(label)
            elif state is not None and not isinstance(state, dict):
                unavailable = True
        if pending:
            attention = True
            lines.append("이전 실패 후 정상 실행 미확인: " + ", ".join(pending))
    except Exception as exc:  # noqa: BLE001
        _log.warning("일일 heartbeat 관측 불가: %s", type(exc).__name__)
        lines.append("마지막 tick 완료: 관측 불가")
        unavailable = True
    try:
        async with get_sessionmaker()() as session:
            queues = await jobs.queue_stats(session)
        totals = {state: sum(item[state] for item in queues.values())
                  for state in ("ready", "running", "dead")}
        lines.append(f"큐: ready {totals['ready']} / running {totals['running']}"
                     f" / actionable dead {totals['dead']} (성공 replay 조상 제외)")
        ages = [item["oldest_dead_age_s"] for item in queues.values()
                if item.get("oldest_dead_age_s") is not None]
        if ages:
            lines.append(f"가장 오래된 미해결 dead: {int(max(ages) / 3600)}시간 경과")
        attention |= totals["dead"] > 0
    except Exception as exc:  # noqa: BLE001
        _log.warning("일일 큐 관측 불가: %s", type(exc).__name__)
        lines.append("큐: 관측 불가")
        unavailable = True
    if rc is None:
        lines.append("RC 미해결: 관측 불가")
        unavailable = True
    else:
        lines.extend(rc_line(env, data, now) for env, data in rc.items())
        attention |= any(data["failed"] + data["pending"] + data.get("provider_unavailable", 0)
                         for data in rc.values())
    if counts is None:
        lines.append("RC 처리기(이번 tick): 관측 불가")
        unavailable = True
    else:
        failures = counts.get("rc_exception", 0) + counts.get("rc_processor_error", 0)
        handled = counts.get("rc_processed", 0) + counts.get("rc_failed", 0) + counts.get("rc_pending", 0)
        lines.append(f"RC 처리기(이번 tick): 결과 {handled}건 / 예외 {failures}건"
                     + (" (처리 대상 없음, provider 정상 검증 아님)" if not handled and not failures else ""))
        attention |= failures > 0
    if counts is not None:
        lines.append(
            f"작업(이번 tick): 일기 {counts.get('diaries', 0)} / 실패 {counts.get('diary_failed', 0)}"
            f" / 시간 초과 {counts.get('timed_out', 0)} / 다음 tick 인계 {counts.get('deadline_skipped', 0)}"
            f" / 해석 불가 timezone {counts.get('tz_unresolvable', 0)}종"
        )
        lines.append(
            f"푸시(이번 tick): 수락 {counts.get('push_sent', 0)}"
            f" / 무효 토큰 {counts.get('push_invalid_token', 0)}"
            f" / 비활성 {counts.get('push_invalidated', 0)}"
            f" / 설정 {counts.get('push_setup_error', 0)}"
            f" / 일시 {counts.get('push_transient', 0)}"
            f" / payload {counts.get('push_payload_error', 0)}"
        )
    try:
        start, end = cost_window(now)
        async with get_sessionmaker()() as session:
            row = (await session.execute(_COST_SQL, {"start": start, "end": end})).mappings().one()
        lines.append(cost_line(dict(row), now))
        unavailable |= row["unknown"] > 0
    except Exception as exc:  # noqa: BLE001
        _log.warning("일일 원가 관측 불가: %s", type(exc).__name__)
        lines.append("AI 원장 추정 비용: 관측 불가")
        unavailable = True
    status = "확인 필요" if attention else "관측한 항목에 미해결 없음"
    if unavailable:
        status = status + " / 일부 관측 불가" if attention else "일부 관측 불가"
    return "\n".join([f"운영점검 — {status} — {now.astimezone(_KST):%Y-%m-%d %H:%M KST}", *lines])


async def emit(now: datetime, counts: dict) -> None:
    """관측·전송 전체를 제한한다. 미통지 사건은 다음 tick에 다시 관측한다."""
    async with asyncio.timeout(_MONITORING_BUDGET_S):
        await _emit(now, counts)


async def _emit(now: datetime, counts: dict) -> None:
    for signal, failed, succeeded in (
        ("diary", counts.get("diary_failed", 0), counts.get("diaries", 0) + counts.get("diary_none", 0)),
        ("memory", counts.get("memory_failed", 0), counts.get("memory_ok", 0)),
        ("timeout", counts.get("timed_out", 0), counts.get("users", 0) - counts.get("timed_out", 0)),
    ):
        tier = failure_tier(failed)
        await monitoring_state.publish(monitoring_state.Observation(
            f"worker_{signal}", True if failed else False if succeeded else None,
            str(tier), f"워커 결과 이상 — {signal} 실패 {failed}건 / 결과 관측 {failed + succeeded}건",
            recovery=f"워커 {signal} 정상 실행 재관측 (과거 결과물 복구 여부는 별도 확인)",
            importance=tier,
        ), now)
    push_sent = counts.get("push_sent", 0)
    push_failed = sum(counts.get(key, 0) for key in (
        "push_setup_error", "push_transient", "push_payload_error",
    ))
    push_outage = push_sent == 0 and push_failed >= settings.worker_push_outage_min
    await monitoring_state.publish(monitoring_state.Observation(
        "worker_push", True if push_outage else False if push_sent else None,
        str(failure_tier(push_failed)),
        f"워커 푸시 전면 실패 — 푸시 실패 {push_failed}(수락 {push_sent})",
        recovery="워커 푸시 수락 재관측 (개별 과거 발송 복구 여부는 별도 확인)",
        importance=failure_tier(push_failed),
    ), now)
    rc = None
    query_failed = False
    try:
        async with get_sessionmaker()() as session:
            rc = await rc_snapshot(session, now)
    except Exception as exc:  # noqa: BLE001
        query_failed = True
        _log.warning("RC 상태 관측 불가: %s", type(exc).__name__)
    await monitoring_state.publish(monitoring_state.Observation(
        "rc_observation", query_failed, "failure", "RC 상태 조회 실패 — 미해결 여부 관측 불가",
        recovery="RC 상태 조회 정상 재관측 (개별 미해결 사건과 별도)",
    ), now)
    processor_failed = bool(counts.get("rc_processor_error") or counts.get("rc_exception"))
    processed = sum(counts.get(key, 0) for key in ("rc_processed", "rc_failed", "rc_pending"))
    await monitoring_state.publish(monitoring_state.Observation(
        "rc_processor", True if processor_failed else False if processed else None, "failure",
        "RC 처리기 실패 — store 환경과 무관하게 확인 필요",
        recovery="RC 이벤트 처리 정상 재관측 (개별 미해결 사건과 별도)",
    ), now)
    if rc is not None:
        unavailable = sum(data.get("provider_unavailable", 0) for data in rc.values())
        await monitoring_state.publish(monitoring_state.Observation(
            "rc_provider", bool(unavailable),
            ":".join(rc[env].get("provider_fingerprint", "empty") for env in sorted(rc)),
            f"RC 외부 이력 확인 불가 {unavailable}건 — store 환경 무관 확인 필요"
            " (provider 전체 장애 확정은 아님)",
            recovery="RC 외부 이력 확인 불가로 남은 사건 0건 재관측",
            importance=failure_tier(unavailable),
        ), now)
        for env in ("PRODUCTION", "UNKNOWN"):
            data = rc[env]
            await monitoring_state.publish(monitoring_state.Observation(
                f"rc_{env.lower()}", bool(data["failed"] + data["pending"]), data["fingerprint"],
                rc_line(env, data, now), recovery=f"RC {env} 미해결 0건 재관측",
            ), now)
    if now.astimezone(_KST).hour >= 13:
        # 날짜는 고정 key의 값에만 저장한다. 전송 실패 시 다음 tick에 다시 시도한다.
        await monitoring_state.publish(monitoring_state.Observation(
            "daily_status", True, now.astimezone(_KST).date().isoformat(),
            "", severity="status", daily=True,
        ), now, render=lambda: _daily_message(now, rc, counts))


def failure_tier(count: int) -> int:
    """tick당 영향 건수를 보수적인 구간으로 묶어 count 흔들림의 알림 폭주를 막는다."""
    return 3 if count >= 20 else 2 if count >= 5 else 1

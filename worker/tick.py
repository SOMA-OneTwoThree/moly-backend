"""배치 틱 — 매시 크론이 호출(멱등). 로컬 04:00 일기 생성 / 09:00 아침·20:00 저녁 푸시."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select

from app.config import settings
from app.core.db import get_sessionmaker
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.models.user_daily_stats import UserDailyStats
from app.services import config_store, diary_generation, memory, notify, slack_notify
from app.services.limits import effective_token_config

_log = logging.getLogger("moly-worker")
DIARY_HOUR = 4  # 로컬 04:00 일기 생성
MORNING_HOUR = 9  # 09:00 아침 일기 푸시
EVENING_HOUR = 20  # 20:00 저녁 안부 푸시
_WORKER_LAST_SUCCESS_KEY = "monitoring:worker_last_success"  # app_config 데드맨 상태 키(health.py와 공유)


_KST = ZoneInfo("Asia/Seoul")
# 자주 보는 타임존의 한국어 나라 라벨(가독성용). 없으면 IANA 이름 그대로.
_TZ_KO = {
    "Asia/Seoul": "한국",
    "Europe/Prague": "체코",
    "Asia/Tokyo": "일본",
    "America/New_York": "미국(동부)",
    "America/Los_Angeles": "미국(서부)",
    "Europe/London": "영국",
}


def _zone_line(tz: str, now: datetime) -> str:
    """'한국(Asia/Seoul) 현지 04:00 · UTC+9' 형태. tz 이상 시 이름만."""
    try:
        local = now.astimezone(ZoneInfo(tz))
        off = local.utcoffset()
        offh = round(off.total_seconds() / 3600) if off else 0
        label = _TZ_KO.get(tz, tz)
        return f"{label}({tz}) 현지 {local:%H:%M} · UTC{offh:+d}"
    except Exception:  # noqa: BLE001  (잘못된 tz라도 요약은 나가야 함)
        return tz


def _build_summary(
    now: datetime, counts: dict, elapsed: float, active_tzs: set[str] | None = None
) -> str:
    """워커 틱 슬랙 요약 메시지 조립. 시각은 한국시간(KST) 우선 + UTC 병기.

    active_tzs = 이 틱에서 일기·아침·저녁을 실제로 처리한 유저들의 타임존(어느 나라 기준인지).
    """
    has_warn = counts["diary_failed"] > 0 or counts["memory_failed"] > 0
    prefix = "⚠️ " if has_warn else ""
    ts_kst = now.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")
    ts_utc = now.strftime("%H:%M UTC")
    diary_fail = f", 실패 ⚠️ {counts['diary_failed']}건" if counts["diary_failed"] else ""
    mem_fail = f" ⚠️ {counts['memory_failed']}" if counts["memory_failed"] else f" {counts['memory_failed']}"
    lines = [f"{prefix}[워커 요약] {ts_kst} ({ts_utc})"]
    if active_tzs:
        zones = " / ".join(_zone_line(tz, now) for tz in sorted(active_tzs))
        lines.append(f"대상 타임존: {zones}")
    lines += [
        f"일기: {counts['diaries']}건 (개인 {counts['diary_llm']} / 프리셋 {counts['diary_preset']}){diary_fail}",
        f"기억(mem0): 성공 {counts['memory_ok']} / 실패{mem_fail}",
        f"푸시: 아침 {counts['morning']}건 / 저녁 {counts['evening']}건",
        f"전체 유저 {counts['users']}명 | 소요 {elapsed:.1f}s",
    ]
    return "\n".join(lines)


async def run_tick(now: datetime | None = None) -> dict[str, int]:
    """이번 틱 처리 건수(일기·아침·저녁)."""
    now = now or datetime.now(timezone.utc)
    counts = {
        "diaries": 0, "diary_llm": 0, "diary_preset": 0, "diary_failed": 0,
        "diary_skipped": 0,  # 이미 생성돼 스킵(멱등 재실행) — 실패와 구분(오탐 방지)
        "memory_ok": 0, "memory_failed": 0,
        "morning": 0, "evening": 0,
        "diary_attempted": 0,  # DIARY_HOUR에 진입한 유저 수(생성·스킵·실패 합산)
        "users": 0,
    }
    active_tzs: set[str] = set()  # 이 틱에서 일기·아침·저녁을 처리한 유저 타임존(요약 표기용)
    start = time.monotonic()
    async with get_sessionmaker()() as session:
        cfg = await effective_token_config(session)
        # 전 프로필 대상(닉네임 유무 무관). 온보딩 전에도 채팅이 되므로 닉네임으로 거르면
        # 대화한 유저가 일기를 영영 못 받는다. timezone은 NOT NULL(기본 Asia/Seoul)이라 안전.
        profiles = list((await session.execute(select(Profile))).scalars().all())
        counts["users"] = len(profiles)
        for p in profiles:
            hour = now.astimezone(ZoneInfo(p.timezone)).hour
            try:
                if hour == DIARY_HOUR:
                    counts["diary_attempted"] += 1
                    active_tzs.add(p.timezone)
                    target = activity_date_for(now, p.timezone) - timedelta(days=1)
                    result = await diary_generation.generate_for_user(session, p, target, cfg)
                    if result.get("created"):
                        counts["diaries"] += 1
                        if result.get("source") == "llm":
                            counts["diary_llm"] += 1
                        else:
                            counts["diary_preset"] += 1
                    elif result.get("skipped"):
                        counts["diary_skipped"] += 1
                    counts["memory_ok"] += result.get("memory_ok", 0)
                    counts["memory_failed"] += result.get("memory_failed", 0)
                elif hour == MORNING_HOUR:
                    active_tzs.add(p.timezone)
                    if await notify.notify_morning(session, p):
                        counts["morning"] += 1
                elif hour == EVENING_HOUR:
                    active_tzs.add(p.timezone)
                    if await notify.notify_evening(session, p):
                        counts["evening"] += 1
            except Exception as e:  # noqa: BLE001  # 한 유저 실패가 배치를 멈추지 않게
                _log.exception("틱 처리 실패(user=%s hour=%s): %r", p.id, hour, e)
                await session.rollback()  # 세션 무효화 방지 — 다음 유저 계속
                if hour == DIARY_HOUR:
                    counts["diary_failed"] += 1

        # 탈퇴 고아 기억 청소(하루 1회, UTC 04시 틱) — vecs는 FK 밖이라 CASCADE 안 닿음(백스톱)
        if now.hour == DIARY_HOUR:
            try:
                counts["swept"] = await memory.sweep_orphans(session)
            except Exception as e:  # noqa: BLE001
                _log.warning("고아 기억 스위프 실패: %r", e)
                await session.rollback()

        # --- 모니터링 상태 기록(반드시 세션 with 블록 안 — 밖은 세션이 닫혀 있음) ---
        # 워커가 끝까지 돌았음을 기록. 데드맨 핑은 '결과 정상' 여부로 별도(_emit_worker_health).
        try:
            await config_store.set_config_value(session, _WORKER_LAST_SUCCESS_KEY, now.isoformat())
        except Exception as e:  # noqa: BLE001  # 기록 실패가 배치를 멈추면 안 됨
            _log.warning("워커 상태 기록 실패: %r", e)
            await session.rollback()
        # 비용 이상치 계산(전일 완결분, 하루 1회 DIARY_HOUR UTC 틱에서만)
        if now.hour == DIARY_HOUR and settings.daily_billable_alert_threshold > 0:
            try:
                counts["billable_yesterday"] = await _sum_billable_yesterday(session, now)
            except Exception as e:  # noqa: BLE001
                _log.warning("전일 billable 합산 실패: %r", e)
                await session.rollback()

    elapsed = time.monotonic() - start

    # 슬랙 요약: 일기 틱(DIARY_HOUR에 진입한 유저 있음) 또는 푸시 발송 있을 때만 전송(빈 틱 스팸 방지)
    if counts["diary_attempted"] + counts["morning"] + counts["evening"] > 0:
        summary = _build_summary(now, counts, elapsed, active_tzs)
        await slack_notify.send_summary(summary)

    # --- 데드맨 핑 + 결과이상/비용 경보(네트워크 — 세션 밖) ---
    await _emit_worker_health(now, counts)

    return counts


async def _sum_billable_yesterday(session, now: datetime) -> int:
    """전일(어제 KST) 완결분 billable 합산. user_daily_stats.tokens_used = 실비용가중 billable 누적.

    messages 풀스캔 대신 작은 집계 테이블 사용. activity_date는 유저별 로컬경계라 근사(비용가드용).
    """
    yday = (now.astimezone(_KST) - timedelta(days=1)).date()
    total = (
        await session.execute(
            select(func.coalesce(func.sum(UserDailyStats.tokens_used), 0)).where(
                UserDailyStats.activity_date == yday
            )
        )
    ).scalar_one()
    return int(total)


async def _emit_worker_health(now: datetime, counts: dict) -> None:
    """데드맨 핑(결과 반영) + 결과이상·비용 경보. 전부 best-effort(실패해도 워커 미중단).

    anomaly = 실패 카운트만으로 판정 — 멱등 재실행의 전원 스킵(diary_skipped)은 정상이라 제외(오탐 방지).
    dedup은 프로세스 내 한정 → 워커는 틱마다 새 프로세스라 지속장애 시 틱당 재알림 감수(스톰은 아님).
    """
    anomaly = counts["diary_failed"] > 0 or counts["memory_failed"] > 0
    if settings.worker_ping_url:
        url = settings.worker_ping_url + ("/fail" if anomaly else "")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.get(url)
        except Exception as e:  # noqa: BLE001
            _log.warning("워커 데드맨 핑 실패: %r", e)
    if anomaly:
        await slack_notify.alert(
            f"⚠️ 워커 결과 이상 — 일기실패 {counts['diary_failed']} / 기억실패 {counts['memory_failed']}",
            dedup_key="worker_anomaly",
        )
    total = counts.get("billable_yesterday")
    thr = settings.daily_billable_alert_threshold
    if total is not None and thr > 0 and total > thr:
        await slack_notify.alert(
            f"💸 전일 billable {total:,} 이 임계 {thr:,} 초과 — 비용 확인 필요", dedup_key="cost_spike"
        )

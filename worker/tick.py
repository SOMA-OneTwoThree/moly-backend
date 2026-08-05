"""배치 틱 — 15분 크론이 호출(멱등, SOMA-348). 로컬 04:00 일기 생성 / 09:00 아침·20:00 저녁 푸시
+ RC 웹훅 inbox 드레인(pending 처리·미해결 failed 재요약, SOMA-372)."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select, text

from app.config import settings
from app.core.db import get_sessionmaker
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.models.revenuecat_event import RevenuecatEvent
from app.models.user_daily_stats import UserDailyStats
from app.services import (
    config_store,
    diary_generation,
    notify,
    push_personalization,
    slack_notify,
    subscription,
)
from app.services.limits import effective_token_config

_RC_INBOX_BATCH = 200            # 틱당 처리할 pending 상한
_RC_DEP_RESERVED = _RC_INBOX_BATCH // 4  # dependency 예약 슬롯(=50) — 신규·재시도 다발에도 굶지 않게
_RC_PENDING_STALE_MIN = 60       # 이보다 오래 pending이면 관측 대상(선행 결제 미도착 등)

_log = logging.getLogger("moly-worker")
DIARY_HOUR = 4  # 로컬 04:00 일기 생성
MORNING_HOUR = 9  # 09:00 아침 일기 푸시
EVENING_HOUR = 20  # 20:00 저녁 안부 푸시


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
    has_warn = counts["diary_failed"] > 0 or counts.get("push_gen_failed", 0) > 0
    prefix = "⚠️ " if has_warn else ""
    ts_kst = now.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")
    ts_utc = now.strftime("%H:%M UTC")
    diary_fail = f", 실패 ⚠️ {counts['diary_failed']}건" if counts["diary_failed"] else ""
    lines = [f"{prefix}[워커 요약] {ts_kst} ({ts_utc})"]
    if active_tzs:
        zones = " / ".join(_zone_line(tz, now) for tz in sorted(active_tzs))
        lines.append(f"대상 타임존: {zones}")
    personalized = (
        f" (개인화 {counts.get('evening_personalized', 0)}건)"
        if counts.get("evening_personalized") else ""
    )
    lines += [
        f"일기: {counts['diaries']}건 (개인 {counts['diary_llm']} / 프리셋 {counts['diary_preset']}"
        f" / 미발행 {counts['diary_none']}){diary_fail}",
        "기억: 상주 consumer 정규화 파이프라인",
        f"푸시: 아침 {counts['morning']}건 / 저녁 {counts['evening']}건{personalized}",
        f"전체 유저 {counts['users']}명 | 소요 {elapsed:.1f}s",
    ]
    gen_total = sum(
        counts.get(k, 0)
        for k in ("push_gen_ok", "push_gen_skipped", "push_gen_rejected",
                  "push_gen_failed", "push_gen_deferred")
    )
    if gen_total:
        fail_mark = " ⚠️" if counts.get("push_gen_failed") else ""
        lines.append(
            f"개인화 생성: 성공 {counts.get('push_gen_ok', 0)}"
            f" / 스킵 {counts.get('push_gen_skipped', 0)}"
            f" / 리젝 {counts.get('push_gen_rejected', 0)}"
            f" / 실패{fail_mark} {counts.get('push_gen_failed', 0)}"
            f" / 이월 {counts.get('push_gen_deferred', 0)}"
        )
    if counts.get("personalized_sent_today") is not None:
        lines.append(f"개인화 발송 누계(오늘·KST 근사): {counts['personalized_sent_today']}건")
    if counts.get("timed_out"):
        lines.append(f"⚠️ 타임아웃 스킵: {counts['timed_out']}건")  # 멈춘 LLM/DB 신호(관측)
    if elapsed > 504:  # 틱 예산 840s(systemd 14분)의 60% — SIGKILL 접근 조기 경보
        lines.append(f"⚠️ 틱 소요 {elapsed:.0f}s — 예산(840s) 60% 초과")
    return "\n".join(lines)


async def _process_user(now: datetime, pid, cfg: dict) -> dict:
    """유저 1명 처리 — 자기 세션(격리). 반환 = 이 유저 partial counts(+active_tz).

    유저별 독립 세션이라 한 유저의 롤백/실패가 다른 유저를 오염시키지 않는다(SOMA-349).
    """
    out = {
        "diaries": 0, "diary_llm": 0, "diary_preset": 0, "diary_none": 0, "diary_failed": 0,
        "diary_skipped": 0, "memory_ok": 0, "memory_failed": 0, "morning": 0, "evening": 0,
        "diary_attempted": 0, "active_tz": None,
        # 저녁 푸시 개인화 — run_tick counts 초기화에도 같은 키가 있어야 한다(병합이
        # `elif k in counts`라 한쪽에만 있으면 조용히 버려진다).
        "push_gen_ok": 0, "push_gen_skipped": 0, "push_gen_rejected": 0,
        "push_gen_failed": 0, "push_gen_deferred": 0, "evening_personalized": 0,
    }
    # 개인화 컨텍스트 — cfg dict에 실어 시그니처 불변(기존 테스트·모킹 호환). 부재 시 off.
    pctx = cfg.get("_push") or push_personalization.TickContext()
    # 하위 호출(일기 생성 등)에는 컨텍스트를 뗀 토큰 설정만 전달 — pctx.rows에 전 유저 문구
    # 스냅샷이 실려 있어, cfg를 그대로 흘리면 로깅·직렬화 한 번에 대화 파생 텍스트가 샌다.
    token_cfg = {k: v for k, v in cfg.items() if k != "_push"}
    async with get_sessionmaker()() as session:
        p = await session.get(Profile, pid)
        if p is None:
            return out  # 틱 도중 탈퇴 — 스킵
        # tz 해석 방어 — 잘못된 timezone 하나가 배치를 무너뜨리지 않게(SOMA-348).
        try:
            local_now = now.astimezone(ZoneInfo(p.timezone))
        except Exception as e:  # noqa: BLE001  # 잘못된/알 수 없는 IANA tz
            _log.warning("틱: 잘못된 timezone %r (user=%s) — 스킵: %r", p.timezone, pid, e)
            return out
        hour = local_now.hour
        try:
            # 신규 로직은 전부 이 try 안(유저 격리) — row_valid 포함(계획 §3 규칙).
            row = pctx.rows.get(pid)
            valid = row is not None and push_personalization.row_valid(row, p, now, pctx.cfg)
            if hour == DIARY_HOUR:
                out["diary_attempted"] = 1
                out["active_tz"] = p.timezone
                target = activity_date_for(now, p.timezone) - timedelta(days=1)
                # 워커 틱 중첩(15분 케이던스·재시도) 시 같은 (유저,날짜) 일기를 두 프로세스가 동시에
                # LLM 생성하지 않도록 커밋된 클레임 행으로 상호배제(SOMA-373). 세션 advisory lock은
                # SQLAlchemy 커넥션 풀 반환·pgbouncer 트랜잭션 풀링과 안 맞아(내부 커밋 시 락이 다른
                # 커넥션으로 새거나 미지원) 클레임 방식을 쓴다. claimed_at 30분 만료로 크래시된 클레임은 회수.
                # 불변식: 만료(30분) ≫ worker_user_timeout_s(120s) — 살아있는 프로세스는 타임아웃돼
                # finally에서 자기 클레임을 먼저 지우므로, 만료 회수는 하드킬(죽은 프로세스)만 대상이다.
                claimed = (
                    await session.execute(
                        text(
                            "INSERT INTO diary_gen_claims (user_id, target_date) VALUES (:u, :d) "
                            "ON CONFLICT (user_id, target_date) DO UPDATE SET claimed_at = now() "
                            "WHERE diary_gen_claims.claimed_at < now() - interval '30 minutes' "
                            "RETURNING 1"
                        ),
                        {"u": pid, "d": target},
                    )
                ).scalar()
                await session.commit()  # 클레임 커밋 — 겹친 틱이 볼 수 있게(가시성)
                if claimed is None:
                    out["diary_skipped"] = 1  # 다른 프로세스가 신선한 클레임 보유 — 중복 LLM 방지
                else:
                    try:
                        result = await diary_generation.generate_for_user(
                            session, p, target, token_cfg
                        )
                        if result.get("created") and result.get("source") != "none":
                            out["diaries"] = 1
                            out["diary_llm" if result.get("source") == "llm" else "diary_preset"] = 1
                        elif result.get("created"):  # tombstone(사용자 노출 X, SOMA-389) — 발행 아님
                            out["diary_none"] = 1
                        elif result.get("skipped"):
                            out["diary_skipped"] = 1  # 멱등 재실행 스킵(실패와 구분, SOMA-301)
                        out["memory_ok"] = result.get("memory_ok", 0)
                        out["memory_failed"] = result.get("memory_failed", 0)
                    finally:
                        # 클레임 해제 — 성공 시 diary 행이 멱등 마커라 삭제 안전, 실패 시 다음 틱 재시도.
                        # generate는 내부 커밋 완료(별도 tx)라 rollback으로 aborted 상태 정리 후 삭제.
                        await session.rollback()
                        await session.execute(
                            text("DELETE FROM diary_gen_claims WHERE user_id = :u AND target_date = :d"),
                            {"u": pid, "d": target},
                        )
                        await session.commit()
            elif hour == push_personalization.GEN_HOUR and pctx.cfg.rollout != "off":
                # 저녁 푸시 개인화 문구 사전 생성(로컬 05시 — 04시 일기 틱과 분리, 실측 근거).
                # rollout 게이트는 생성에도 적용: off = LLM 호출·쿼리 0(배포=변화 0, SQL 1줄
                # 롤백이 비용까지 멈춘다). allowlist 모드는 대상 유저만 생성(카나리 중 전체
                # 유저 비용 방지). 첫 효과는 설정 후 다음 05시.
                if push_personalization.user_allowed(pid, pctx.cfg):
                    if pctx.budget_exceeded():
                        # 틱 예산(420s) 소진 — 남은 유저는 다음 틱(:15/:30/:45)이 멱등 승계.
                        # systemd 840s 하드킬 전에 스스로 멈추는 것이 유일한 구조적 방어.
                        out["push_gen_deferred"] = 1
                        out["active_tz"] = p.timezone
                    else:
                        status = await push_personalization.generate_for_user(
                            session, p, now, pctx.cfg, token_cfg
                        )
                        key = {
                            "ok": "push_gen_ok", "rejected": "push_gen_rejected",
                            "failed": "push_gen_failed", "skipped": "push_gen_skipped",
                        }.get(status)
                        if key:  # already/busy/no_target = 멱등 재실행·비대상(무카운트)
                            out[key] = 1
                            out["active_tz"] = p.timezone
            elif hour == MORNING_HOUR:
                out["active_tz"] = p.timezone
                if await notify.notify_morning(session, p, now):
                    out["morning"] = 1
            elif hour == EVENING_HOUR:
                out["active_tz"] = p.timezone
                sent_p = 0
                if valid and not await push_personalization.chatted_today(session, p, now):
                    if row.send_slot == push_personalization.SLOT_NIGHT:
                        # 야간 코호트(slot=20:00): 기존 저녁 분기 안에서 인라인 처리.
                        sent_p = await notify.notify_evening_personalized(session, p, row, now)
                    else:
                        # slot ≤ 19:45 코호트: 창(slot+60분)이 20시대로 걸치면 개인화 재시도.
                        slot_dt = datetime.combine(
                            local_now.date(), row.send_slot, tzinfo=local_now.tzinfo
                        )
                        if slot_dt <= local_now < slot_dt + timedelta(minutes=60):
                            sent_p = await notify.notify_evening_personalized(
                                session, p, row, now
                            )
                    # 20시대는 매 틱이 백스톱: 개인화가 못 나갔으면(창 밖·창 안 실패 불문)
                    # 같은 틱에서 즉시 디폴트 시도. "창 만료(slot+60m)까지 대기" 조건을 걸면
                    # slot=19:45 코호트의 백스톱이 20:45 틱 1회로 좁아져 그 틱이 밀리면 그날
                    # 무발송이 된다(리뷰 M2 실측). claim(evening_notified_at) 공유라 이중발송
                    # 불가 — 개인화가 claim 후 실패한 경우엔 디폴트 claim이 False로 멱등 스킵
                    # (그날 미발송 수용, 기존 디폴트와 동일 리스크).
                    if not sent_p and await notify.notify_evening(session, p, now):
                        out["evening"] = 1
                elif await notify.notify_evening(session, p, now):
                    # 개인화 무효·오늘 이미 대화 → 기존 디폴트 그대로(rollout off면 항상 여기).
                    out["evening"] = 1
                if sent_p:
                    out["evening_personalized"] = 1
            # ── 개인화 슬롯 창(저녁 20시 외 시간대, 독립 분기) ──────────────────────
            # slot ∈ [08:00, 19:45]는 자기 슬롯 시각(첫 대화 시각)에 발송한다. 60분 창 ×
            # 15분 케이던스 = 최대 4회 시도, 실패분은 20시 디폴트 백스톱이 받는다(위).
            # 야간 코호트(20:00)는 EVENING 분기가 전담. MORNING(09시)과 겹쳐도 클레임이
            # 다르고 아침 푸시는 킬스위치 off 상태라 이중발송 표면 없음.
            if (
                hour != EVENING_HOUR
                and valid
                and row.send_slot != push_personalization.SLOT_NIGHT
            ):
                slot_dt = datetime.combine(
                    local_now.date(), row.send_slot, tzinfo=local_now.tzinfo
                )
                if (
                    slot_dt <= local_now < slot_dt + timedelta(minutes=60)
                    and not await push_personalization.chatted_today(session, p, now)
                ):
                    if await notify.notify_evening_personalized(session, p, row, now):
                        out["evening_personalized"] = 1
                        out["active_tz"] = p.timezone
        except Exception as e:  # noqa: BLE001  # 한 유저 실패가 배치를 멈추지 않게
            _log.exception("틱 처리 실패(user=%s hour=%s): %r", pid, hour, e)
            await session.rollback()
            if hour == DIARY_HOUR:
                out["diary_failed"] = 1
            elif hour == push_personalization.GEN_HOUR and pctx.cfg.rollout != "off":
                # generate_for_user 앞단(조회·클레임)에서 새는 계통 장애도 카운트해야
                # 요약·anomaly가 무음이 되지 않는다(리뷰 M1 — 05시 전면 실패 무음 방지).
                out["push_gen_failed"] = 1
    return out


def _priority_drain_stmt():
    """우선순위 후보 select — 공정 정렬(신규→예외재시도→dependency_missing→received_at).

    분류 기준(스키마 추가 없이): 신규=last_error NULL. 예외 재시도=attempts>0(_record_retry가 증가).
    dependency_missing=attempts=0이면서 last_error 기록(_record_terminal은 attempts 불변). 이 순서로
    LIMIT을 채워 신규·예외재시도가 dependency 다발에 굶지 않게 보장한다(SOMA-372 §11.4).
    """
    return (
        select(RevenuecatEvent.event_id)
        .where(
            RevenuecatEvent.status == "pending",
            RevenuecatEvent.next_attempt_at <= func.now(),  # backoff 미도래 행 제외(rotation)
        )
        .order_by(
            RevenuecatEvent.last_error.is_(None).desc(),  # 1) 신규(오류 미기록)
            (RevenuecatEvent.attempts > 0).desc(),         # 2) 예외 재시도(attempts>0)
            RevenuecatEvent.next_attempt_at,               # 3) 재시도 예약 이른 순(rotation)
            RevenuecatEvent.received_at,                    # 4) 동률이면 오래된 순
        )
        .limit(_RC_INBOX_BATCH)
    )


def _dependency_quota_stmt():
    """dependency_missing 예약분 — 가장 오래된 순(aging). 신규·재시도가 매 틱 배치를 가득 채워도
    이 예약 슬롯(_RC_DEP_RESERVED)만큼은 항상 오래된 dependency가 선택돼 무기한 굶지 않는다.

    dependency_missing = attempts=0(재시도 아님) AND last_error 기록(신규 아님).
    """
    return (
        select(RevenuecatEvent.event_id)
        .where(
            RevenuecatEvent.status == "pending",
            RevenuecatEvent.attempts == 0,
            RevenuecatEvent.last_error.isnot(None),
            RevenuecatEvent.next_attempt_at <= func.now(),  # backoff 미도래 행 제외(rotation)
        )
        # 재시도 예약 이른 순 → 재-pending으로 밀린 행은 후순위, 미선택 dependency가 먼저(aging·rotation)
        .order_by(RevenuecatEvent.next_attempt_at, RevenuecatEvent.received_at)
        .limit(_RC_DEP_RESERVED)
    )


async def _select_pending_ids(session) -> list[str]:
    """이번 틱 처리 대상 event_id — 공정 선택(SOMA-372 §11.4).

    dependency 예약 슬롯을 먼저 확보(가장 오래된 dependency)한 뒤 나머지를 우선순위(신규→재시도→
    dependency)로 채운다. 신규·재시도가 배치를 초과해도 dependency는 예약분만큼 처리되고, dependency
    다발이 와도 나머지 슬롯이 신규·재시도로 채워져 어느 쪽도 굶지 않는다. 전체는 _RC_INBOX_BATCH 이하.
    """
    dep_ids = list((await session.execute(_dependency_quota_stmt())).scalars().all())
    prio_ids = list((await session.execute(_priority_drain_stmt())).scalars().all())
    dep_set = set(dep_ids)
    remaining = _RC_INBOX_BATCH - len(dep_ids)
    merged = dep_ids + [e for e in prio_ids if e not in dep_set][:remaining]
    return merged


async def _drain_rc_inbox(now: datetime) -> dict:
    """RC 웹훅 inbox 드레인 — pending 각각 독립 트랜잭션으로 process_event(이벤트별 격리).

    + 미해결 failed·장기 pending을 매 틱 Slack 재요약(1회성 알림 유실 방지 — slack_notify가
    URL 미설정·전송실패를 삼키므로 매 틱 재요약해 은폐 없이 관측한다, SOMA-372 §11.4).
    """
    out = {"rc_processed": 0, "rc_failed": 0, "rc_pending": 0, "rc_exception": 0}
    # 1) pending 후보 처리 — 한 행씩 claim(FOR UPDATE SKIP LOCKED는 process_event 내부).
    # 공정 선택으로 양방향 starvation 방지: dependency 예약 슬롯(_RC_DEP_RESERVED)을 먼저 확보한 뒤
    # 나머지를 우선순위(신규→예외재시도→dependency)로 채운다. 신규·재시도가 배치를 초과해도 오래된
    # dependency가 예약분만큼 처리되고, dependency 다발이 와도 신규·재시도가 굶지 않는다(SOMA-372 §11.4).
    async with get_sessionmaker()() as s:
        ids = await _select_pending_ids(s)
    for eid in ids:
        async with get_sessionmaker()() as s:  # 이벤트별 독립 트랜잭션
            try:
                res = await subscription.process_event(s, eid)
                if res in ("handled", "no_op"):
                    out["rc_processed"] += 1
                elif res in ("permanent_failure", "transfer"):
                    out["rc_failed"] += 1
                elif res == "exception":
                    out["rc_exception"] += 1
                else:  # dependency_missing / skipped
                    out["rc_pending"] += 1
            except Exception as e:  # noqa: BLE001  # 한 이벤트 실패가 드레인을 멈추지 않게
                _log.exception("RC inbox 드레인 실패(event=%s): %r", eid, e)
                await s.rollback()
    # 2) 미해결 failed·장기 pending 재요약(dedup 창 < 틱 간격이라 매 틱 발송).
    # failed는 전체 건수를 count로 집계(표본만 세면 20+ 미해결이 "20건"으로 축소돼 은폐되므로),
    # 최신 표본만 본문에 첨부한다(SOMA-372 §11.4 — 은폐 없이 관측).
    async with get_sessionmaker()() as s:
        failed_total = (
            await s.execute(
                select(func.count())
                .select_from(RevenuecatEvent)
                .where(RevenuecatEvent.status == "failed")
            )
        ).scalar() or 0
        failed_sample = list(
            (
                await s.execute(
                    select(RevenuecatEvent)
                    .where(RevenuecatEvent.status == "failed")
                    .order_by(RevenuecatEvent.received_at.desc())  # 최신 표본
                    .limit(10)
                )
            ).scalars().all()
        )
        stale_pending = (
            await s.execute(
                select(func.count())
                .select_from(RevenuecatEvent)
                .where(
                    RevenuecatEvent.status == "pending",
                    RevenuecatEvent.received_at
                    < now - timedelta(minutes=_RC_PENDING_STALE_MIN),
                )
            )
        ).scalar() or 0
    if failed_total or stale_pending:
        await slack_notify.alert(
            _rc_inbox_summary(failed_sample, int(failed_total), int(stale_pending), now),
            dedup_key="rc_inbox_unresolved",
        )
    return out


def _rc_inbox_summary(
    failed_sample: list, failed_total: int, stale_pending: int, now: datetime
) -> str:
    """미해결 RC 웹훅 요약 — 전체 failed 건수 + 최신 표본(event_id·app_user_id·type·last_error)."""
    ts_kst = now.astimezone(_KST).strftime("%Y-%m-%d %H:%M KST")
    lines = [
        f"⚠️ [RC 웹훅 inbox 미해결] {ts_kst}",
        f"failed {failed_total}건 / 장기 pending(>{_RC_PENDING_STALE_MIN}m) {stale_pending}건",
    ]
    for ev in failed_sample[:10]:
        payload = ev.payload or {}
        lines.append(
            f"· {ev.event_id} type={payload.get('type')} "
            f"app_user_id={payload.get('app_user_id')} err={(ev.last_error or '')[:120]}"
        )
    lines.append("→ 운영 수동 처리 필요(TRANSFER·미등록 상품·거래ID 누락 등).")
    return "\n".join(lines)


async def _profile_id_batches(batch_size: int):
    """프로필 id를 키셋 페이지네이션으로 배치 단위 yield — 전량 메모리 적재를 피한다(SOMA-349)."""
    last = None
    while True:
        async with get_sessionmaker()() as s:
            q = select(Profile.id).order_by(Profile.id).limit(batch_size)
            if last is not None:
                q = q.where(Profile.id > last)
            pids = list((await s.execute(q)).scalars().all())
        if not pids:
            return
        yield pids
        if len(pids) < batch_size:
            return
        last = pids[-1]


async def run_tick(now: datetime | None = None) -> dict[str, int]:
    """이번 틱 처리 건수(일기·아침·저녁).

    유저별 독립 세션 + 배치 페이지네이션 + 유저별 타임아웃 + 상한 동시성(SOMA-349).
    한 유저의 지연·실패가 배치 전체를 막지 않는다. 동시성 상한은 config(기본 1=실질 순차).
    """
    now = now or datetime.now(timezone.utc)
    counts = {
        "diaries": 0, "diary_llm": 0, "diary_preset": 0, "diary_none": 0, "diary_failed": 0,
        "diary_skipped": 0,  # 이미 생성돼 스킵(멱등 재실행) — 실패와 구분(오탐 방지)
        "morning": 0, "evening": 0,
        "diary_attempted": 0,  # DIARY_HOUR에 진입한 유저 수(생성·스킵·실패 합산)
        "timed_out": 0,        # 유저별 타임아웃으로 스킵된 수(관측)
        "users": 0,
        "rc_processed": 0, "rc_failed": 0, "rc_pending": 0, "rc_exception": 0,  # RC inbox 드레인
        # 저녁 푸시 개인화(_process_user out과 키 집합 동기 필수 — 병합이 elif k in counts)
        "push_gen_ok": 0, "push_gen_skipped": 0, "push_gen_rejected": 0,
        "push_gen_failed": 0, "push_gen_deferred": 0, "evening_personalized": 0,
    }
    active_tzs: set[str] = set()  # 이 틱에서 일기·아침·저녁을 처리한 유저 타임존(요약 표기용)
    start = time.monotonic()
    sem = asyncio.Semaphore(max(1, settings.worker_max_concurrency))
    timeout = settings.worker_user_timeout_s

    async with get_sessionmaker()() as s0:
        cfg = await effective_token_config(s0)
        # 저녁 푸시 개인화 프리페치 — 전체 try/except: 어떤 실패도 기존 경로를 건드리지 않는다
        # (실패 = off 컨텍스트 = 전원 디폴트). rollout off면 내부에서 쿼리 없이 빈 맵.
        pctx = push_personalization.TickContext(tick_start=start)
        try:
            pcfg = await push_personalization.effective_push_config(s0)
            pctx = push_personalization.TickContext(
                cfg=pcfg,
                rows=await push_personalization.prefetch_rows(s0, now, pcfg),
                tick_start=start,
            )
        except Exception as e:  # noqa: BLE001  # 프리페치 실패가 푸시 전체를 막으면 안 됨
            _log.warning("개인화 프리페치 실패(전원 디폴트 폴백): %r", e)
    cfg = {**cfg, "_push": pctx}  # 시그니처 불변 전달(_process_user가 꺼내 씀)

    async def _guarded(pid) -> dict:
        async with sem:  # 동시 실행 유저 수 상한
            try:
                return await asyncio.wait_for(_process_user(now, pid, cfg), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                _log.warning("틱: 유저 처리 타임아웃(user=%s, %.0fs) — 스킵", pid, timeout)
                return {"timed_out": 1}

    async for pids in _profile_id_batches(settings.worker_batch_size):
        counts["users"] += len(pids)
        # return_exceptions: _process_user·_guarded가 예외를 삼키지만, 여기서 새는 예외 하나가
        # 남은 배치 전원 + RC 드레인 + 하트비트 + 요약을 전부 죽이는 단일 실패점이 되지 않게 최후 방어.
        results = await asyncio.gather(
            *(_guarded(pid) for pid in pids), return_exceptions=True
        )
        for r in results:
            if isinstance(r, BaseException):
                _log.error("틱: 유저 처리 미포착 예외(무시): %r", r)
                continue
            for k, v in r.items():
                if k == "active_tz":
                    if v:
                        active_tzs.add(v)
                elif k in counts:
                    counts[k] += v

    # RC 웹훅 inbox 드레인 — 매 틱(15분). 유저 처리와 독립(전용 세션·이벤트별 트랜잭션).
    try:
        for k, v in (await _drain_rc_inbox(now)).items():
            counts[k] = counts.get(k, 0) + v
    except Exception as e:  # noqa: BLE001  # 드레인 실패가 배치 전체를 막으면 안 됨
        _log.exception("RC inbox 드레인 틱 실패(무시): %r", e)

    # 워커가 끝까지 돌았음을 매 틱 기록 — /health/deep의 stale(2h) 판정 근거.
    # DIARY_HOUR 블록 안에 있으면 하루 1회만 갱신돼 나머지 22시간이 오탐 stale이 된다.
    # (데드맨 핑은 결과 정상 여부로 별도, _emit_worker_health)
    try:
        async with get_sessionmaker()() as s_hb:
            await config_store.set_config_value(
                s_hb, config_store.WORKER_LAST_SUCCESS_KEY, now.isoformat()
            )
    except Exception as e:  # noqa: BLE001  # 기록 실패가 배치를 멈추면 안 됨
        _log.warning("워커 상태 기록 실패: %r", e)

    # 하루 1회(UTC 04시 틱): 비용 기록.
    # (SOMA-349에서 유저별 세션으로 바뀌어 공유 session이 없으므로 전용 세션을 연다.)
    if now.hour == DIARY_HOUR:
        async with get_sessionmaker()() as smon:
            # 전일 완결분 billable 합산(임계 비교·경보는 _emit_worker_health)
            if settings.daily_billable_alert_threshold > 0:
                try:
                    counts["billable_yesterday"] = await _sum_billable_yesterday(smon, now)
                except Exception as e:  # noqa: BLE001
                    _log.warning("전일 billable 합산 실패: %r", e)
                    await smon.rollback()

    elapsed = time.monotonic() - start

    # 슬랙 요약: 실제 작업(일기 생성/실패 또는 푸시 발송)이 있을 때만 전송(빈 틱 스팸 방지).
    # diary_attempted 기준이면 15분 케이던스에서 DIARY_HOUR 시간대마다 요약이 4번 나가고
    # 그중 3번은 _diary_exists skip이라 "일기 0건" — 감시 채널이 오탐으로 도배된다(SOMA-348 후속).
    # diary_none(tombstone)은 사용자 노출 0이라 요약 트리거에서 제외 — 지정본 없는 조용한 날에
    # 매 틱 요약이 나가 채널이 도배되는 걸 막는다(SOMA-389, 위 오탐 방지 취지 유지).
    # 개인화 생성(05시) 활동은 트리거에 포함 — 없으면 생성 전면 실패가 무음이 된다.
    # 단 evening_personalized(슬롯 발송)는 트리거 제외: 발송 시각이 08:00~20:00에 흩어져
    # 매 틱 요약 도배가 되므로, 누계는 아래 DB 근사 라인이 20시 요약에 싣는다.
    gen_activity = (
        counts["push_gen_ok"] + counts["push_gen_skipped"] + counts["push_gen_rejected"]
        + counts["push_gen_failed"] + counts["push_gen_deferred"]
    )
    # KST 20시대 틱만은 개인화 발송도 트리거로 인정 — 전원이 개인화로 나가 디폴트 0건이면
    # "개인화 발송 누계" 상시 신호(§4)를 실을 요약 자체가 안 나가는 구멍 방지(리뷰 Minor-5).
    # 그 외 시간대의 슬롯 발송은 트리거 제외 유지(하루 96틱 도배 방지 — 누계 라인이 대신함).
    kst_evening_personalized = (
        counts["evening_personalized"]
        if now.astimezone(_KST).hour == EVENING_HOUR else 0
    )
    if (
        counts["diaries"] + counts["diary_failed"] + counts["morning"] + counts["evening"]
        + gen_activity + kst_evening_personalized
    ) > 0:
        # 상시 신호(조용한 전원 디폴트 회귀 감지): 개인화 발송 누계(KST 활동일 근사, best-effort).
        if pctx.cfg.rollout != "off" and (counts["evening"] or counts["evening_personalized"]):
            try:
                async with get_sessionmaker()() as s_cnt:
                    counts["personalized_sent_today"] = (
                        await push_personalization.sent_count_for(
                            s_cnt, activity_date_for(now, "Asia/Seoul")
                        )
                    )
            except Exception as e:  # noqa: BLE001
                _log.warning("개인화 발송 누계 조회 실패(요약 생략): %r", e)
        summary = _build_summary(now, counts, elapsed, active_tzs)
        await slack_notify.send_summary(summary)

    # --- 데드맨 핑 + 결과이상/비용 경보(네트워크 — 세션 밖) ---
    # 최후 방어: 모니터링은 무슨 일이 있어도 배치 틱을 깨면 안 된다(일기·푸시는 이미 커밋됨).
    try:
        await _emit_worker_health(now, counts)
    except Exception as e:  # noqa: BLE001
        _log.warning("워커 헬스 emit 실패(무시): %r", e)

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
    # memory_failed는 counts 초기화에서 빠져 병합이 버린다(상주 consumer 전환 잔재) —
    # 직접 인덱싱하면 KeyError로 데드맨 핑 자체가 죽어 왔으므로 .get으로 방어한다.
    gen_attempted = (
        counts.get("push_gen_ok", 0) + counts.get("push_gen_rejected", 0)
        + counts.get("push_gen_failed", 0)
    )
    # 개인화 생성 anomaly: 실패가 있거나, 유의미한 표본(≥3)이 전부 리젝/실패(ok 0) —
    # 모델 은퇴·검수 포맷 변경 등으로 전원이 조용히 디폴트 회귀하는 걸 당일 잡는 신호.
    # 최소 표본 조건이 없으면 allowlist 카나리(1~2명)에서 정상 동작인 리젝 1건이
    # 데드맨을 /fail로 뒤집어 신호가 죽는다(리뷰 M4).
    push_gen_anomaly = counts.get("push_gen_failed", 0) > 0 or (
        gen_attempted >= 3 and counts.get("push_gen_ok", 0) == 0
    )
    core_anomaly = counts["diary_failed"] > 0 or counts.get("memory_failed", 0) > 0
    anomaly = core_anomaly or push_gen_anomaly
    if settings.worker_ping_url:
        url = settings.worker_ping_url + ("/fail" if anomaly else "")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.get(url)
        except Exception as e:  # noqa: BLE001
            _log.warning("워커 데드맨 핑 실패: %r", e)
    if core_anomaly:
        await slack_notify.alert(
            f"⚠️ 워커 결과 이상 — 일기실패 {counts['diary_failed']}"
            f" / 기억실패 {counts.get('memory_failed', 0)}",
            dedup_key="worker_anomaly",
        )
    if push_gen_anomaly:  # 일기 이상과 dedup을 분리 — 서로를 억제하지 않게(계획 §4)
        await slack_notify.alert(
            f"⚠️ 개인화 생성 이상 — 실패 {counts.get('push_gen_failed', 0)}"
            f" / 리젝 {counts.get('push_gen_rejected', 0)}"
            f" / 성공 {counts.get('push_gen_ok', 0)}",
            dedup_key="push_personalization_anomaly",
        )
    total = counts.get("billable_yesterday")
    thr = settings.daily_billable_alert_threshold
    if total is not None and thr > 0 and total > thr:
        await slack_notify.alert(
            f"💸 전일 billable {total:,} 이 임계 {thr:,} 초과 — 비용 확인 필요", dedup_key="cost_spike"
        )

"""알림 발송 조립 — 설정 확인 → 기기 토큰 로드 → FCM 발송. 워커가 현지 09·10·20시에 호출.

- 09시 루틴 아침: 오늘 할 루틴이 있고 아직 하나도 안 한 사람. 요일 문구, 직전 루틴 날에 스트릭이 깨졌으면 rm.break.
- 10시 아침 일기: 어제 일기가 오늘 공개됐고 안 읽은 사람.
- 20시 저녁: 운영 공지 → 스트릭 위기 → 캐피 대화 중 처음 맞는 하나. 대화 문구는 유저 상태 갈래에서 고른다.
문구는 push_copy.py. 분기 신호는 발송 순간에 읽는다. 대화·일기 날짜는 활동일(로컬 04시 경계),
루틴 날짜는 루틴 기능과 같은 보상일(로컬 00시 경계)이다. 서버 푸시는 그날 21시 이후 도착하지 않는다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import activity_date_for, current_activity_date, reward_date_for, safe_zone
from app.models.chat_context import ChatContext
from app.models.diary import Diary
from app.models.routine import RoutineCompletion
from app.models.user_daily_stats import UserDailyStats
from app.models.user_device import UserDevice
from app.models.user_notification_settings import UserNotificationSettings
from app.services import i18n, push, push_copy, routine, routine_records
from app.services.config_store import get_config_values

_log = logging.getLogger("moly-worker")

# 현지 발송 시각. worker/tick.py가 이 값으로 처리 시각을 정한다.
ROUTINE_MORNING_HOUR = 9
DIARY_MORNING_HOUR = 10
EVENING_HOUR = 20
# 서버 푸시는 이 시각(현지) 이후 도착하지 않는다 — 만료 = min(보낸 시각 + 2시간, 그날 21시).
PUSH_CUTOFF_HOUR = 21
_PUSH_TTL = timedelta(hours=2)

# 마지막 대화가 이 안이면 저녁 대화 푸시를 보내지 않는다(대화 중). 다음 틱에 다시 본다.
ACTIVE_CHAT_WINDOW = timedelta(minutes=60)

# 가입 후 이 일수(활동일 차) 이내 + 대화 이력 없음 = first_touch. 지나면 default_long.
FIRST_TOUCH_MAX_DAYS = 7

# app_config 키 — 서버 푸시 종류별 켜기·끄기. value(jsonb) 예: {"routine_morning": true, "routine_risk": true}
# 없는 키는 기본값. DB 값만 바꾸면 재배포 없이 다음 틱부터 적용된다.
PUSH_SETTINGS_KEY = "push_settings"
_PUSH_DEFAULTS = {"routine_morning": False, "routine_risk": False}

# app_config 키 — 저녁 푸시 1회성 오버라이드(공지). value(jsonb) 형식:
# {"date": "YYYY-MM-DD", "ko": ["제목", "본문"], "en": [...], "ja": [...]}
# 유저의 활동일이 date와 같으면 카테고리 분기 대신 이 문구로 발송한다. 운영은 쿼리만으로
# (INSERT=예약 / DELETE=취소, 재배포 없음). 날짜가 지나면 자동으로 평소 동작 복귀.
OVERRIDE_KEY = "evening_push_override"
OVERRIDE_CATEGORY = "override"
ROUTINE_RISK_CATEGORY = "routine_risk"

# 저녁 종류별 발송 카운트 키(관측) — tick의 counts 초기화가 이 목록으로 만든다.
# "fallback" = 신호 조회 실패로 중립 문구가 나간 건(분포 이상 감지용).
EVENING_STAT_KEYS: tuple[str, ...] = push_copy.CATEGORIES + (
    "fallback", OVERRIDE_CATEGORY, ROUTINE_RISK_CATEGORY,
)


def _local_hour(now: datetime, tz: str) -> int:
    return now.astimezone(safe_zone(tz)).hour


def _expires_at(now: datetime, tz: str) -> datetime:
    """min(보낸 시각 + 2시간, 그날 현지 21시). 휴대폰이 늦게 켜져도 밤늦게 도착하지 않는다."""
    cutoff = now.astimezone(safe_zone(tz)).replace(
        hour=PUSH_CUTOFF_HOUR, minute=0, second=0, microsecond=0,
    )
    return min(now + _PUSH_TTL, cutoff).astimezone(timezone.utc)


async def _push_on(session: AsyncSession, key: str) -> bool:
    """app_config `push_settings`의 켜기·끄기. 키가 없거나 읽기에 실패하면 기본값."""
    try:
        async with session.begin_nested():
            cfg = (await get_config_values(session, [PUSH_SETTINGS_KEY])).get(PUSH_SETTINGS_KEY)
    except Exception:  # noqa: BLE001
        _log.warning("푸시 설정 읽기 실패 — 기본값(%s)", key, exc_info=True)
        return _PUSH_DEFAULTS[key]
    value = cfg.get(key) if isinstance(cfg, dict) else None
    return value if isinstance(value, bool) else _PUSH_DEFAULTS[key]


async def _enabled(session: AsyncSession, uid, type_: str) -> bool:
    row = (
        await session.execute(
            select(UserNotificationSettings).where(
                UserNotificationSettings.user_id == uid,
                UserNotificationSettings.type == type_,
            )
        )
    ).scalars().first()
    return row.enabled if row is not None else True  # 행 없으면 on(기본)


def _live_token_filter():
    """FCM이 무효 확정한 행(invalidated_at)은 건너뛴다. 단, 그 뒤 앱이 같은 토큰을 다시 등록해
    moly-auth upsert가 last_active_at을 더 최신으로 갱신했으면 되살린다 — 잘못 표시된 토큰의
    자가 복구 경로(moly-auth 변경 없이 성립). 다시 실패하면 다음 발송에서 다시 표시된다."""
    return or_(
        UserDevice.invalidated_at.is_(None),
        UserDevice.last_active_at > UserDevice.invalidated_at,
    )


async def _tokens(session: AsyncSession, uid) -> list[str]:
    return list(
        (
            await session.execute(
                select(UserDevice.push_token).where(UserDevice.user_id == uid, _live_token_filter())
            )
        ).scalars().all()
    )


# 발송 결과 관측 키 — tick의 counts/out 초기화가 이 목록으로 만든다(없는 키는 병합에서 버려짐).
PUSH_STAT_KEYS: tuple[str, ...] = (
    "push_sent",           # FCM이 수락한 요청 수(토큰 단위)
    "push_invalid_token",  # FCM이 무효 확정한 토큰 수(UNREGISTERED·토큰 INVALID_ARGUMENT·SENDER_ID_MISMATCH)
    "push_invalidated",    # 그중 user_devices.invalidated_at을 실제로 찍은 행 수
    "push_payload_error",  # 400인데 토큰 필드가 아님 — 우리 메시지 버그 신호
    "push_setup_error",    # 401 APNs 키 · 403 권한 · 404 프로젝트 — 전 토큰 공통 장애 신호
    "push_transient",      # 429·5xx·네트워크
)


def _account_push(stats: dict | None, result) -> None:
    """push.send 결과를 관측 카운트에 얹는다. int fake(테스트)가 와도 안전(getattr 기본값)."""
    if stats is None:
        return
    stats["push_sent"] = stats.get("push_sent", 0) + int(result)
    for kind, n in getattr(result, "failures", {}).items():
        key = f"push_{kind}"
        stats[key] = stats.get(key, 0) + n


def _count_push_setup_failure(stats: dict | None) -> None:
    """발송 준비 또는 push.send 예외를 설정 오류로 집계한다.
    FCM 응답 분류 밖에서 발생하는 실패도 워커의 전면 장애 판정에 포함한다."""
    if stats is not None:
        stats["push_setup_error"] = stats.get("push_setup_error", 0) + 1


async def _invalidate_dead_tokens(session: AsyncSession, uid, tokens, *, stats: dict | None) -> int:
    """FCM이 '토큰 자체가 무효'라고 확정한 행에 invalidated_at을 찍는다(행 삭제 아님).

    user_devices의 DDL은 이 레포(db/schema.sql)가 갖고, 평소 쓰기는 moly-auth가 한다(등록 =
    push_token 충돌 시 재귀속 upsert, 로그아웃 = (push_token, user_id) delete). 여기서도 같은
    (user_id, push_token) 쌍으로 좁혀 표시하므로 그 사이 다른 유저로 재귀속된 행은 건드리지 않고,
    moly-auth의 등록·로그아웃 동작은 그대로다(이 컬럼을 모른 채 upsert/delete). 되살리기는
    _live_token_filter(재등록 시 last_active_at > invalidated_at)가 맡는다.
    settings.fcm_invalidate_dead_tokens=False(기본)면 집계·로그만 남긴다(드라이런).
    표시 실패는 발송 결과를 뒤집지 않는다 — 다음 틱에 같은 토큰이 다시 무효로 돌아온다.
    """
    tokens = list(dict.fromkeys(tokens))
    if not tokens or session is None:
        return 0
    if not settings.fcm_invalidate_dead_tokens:
        _log.info("FCM 무효 토큰 %d건 — 비활성 표시 꺼짐(드라이런, user=%s)", len(tokens), uid)
        return 0
    try:
        res = await session.execute(
            update(UserDevice)
            .where(UserDevice.user_id == uid, UserDevice.push_token.in_(tokens))
            .values(invalidated_at=func.now())
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        _log.warning("FCM 무효 토큰 표시 실패(user=%s)", uid, exc_info=True)
        await session.rollback()
        return 0
    marked = res.rowcount if res.rowcount is not None and res.rowcount >= 0 else len(tokens)
    if stats is not None:
        stats["push_invalidated"] = stats.get("push_invalidated", 0) + marked
    _log.info("FCM 무효 토큰 %d건 비활성 표시(user=%s)", marked, uid)
    return marked


async def _claim_send_slot(
    session: AsyncSession, profile, column: str, *, now: datetime | None = None,
    values: dict | None = None,
) -> bool:
    """유저×활동일당 해당 알림을 최초 1회만 '선점'(atomic upsert). 이미 발송했으면 False.

    발송 전에 마커를 선점(at-most-once) — 재시도·중복 실행·15분 케이던스에서 중복 푸시 방지.
    발송 실패 시 그날은 스킵되나(마커 잔존), 스팸보다 낫다(알림은 on/off 베스트에포트).
    활동일(로컬 04:00 경계) 기준 — 09·10·20시 모두 당일에 귀속된다(이 시각엔 보상일과도 같다).
    values = 선점과 함께 남길 칸(저녁 문구 id `evening_push_variant` 등).
    """
    ad = (
        activity_date_for(now, profile.timezone)
        if now is not None else current_activity_date(profile.timezone)
    )
    col = getattr(UserDailyStats, column)
    marks = {column: func.now(), **(values or {})}
    stmt = (
        pg_insert(UserDailyStats)
        .values(user_id=profile.id, activity_date=ad, **marks)
        .on_conflict_do_update(
            index_elements=["user_id", "activity_date"],
            set_=marks,
            where=col.is_(None),  # 이미 발송(NOT NULL)이면 갱신 안 함 → RETURNING 비어 skip
        )
        .returning(UserDailyStats.id)
    )
    claimed = (await session.execute(stmt)).scalars().first() is not None
    await session.commit()
    return claimed


async def _morning_diary_id(session: AsyncSession, profile, now: datetime):
    local = now.astimezone(safe_zone(profile.timezone))
    today_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (await session.execute(
        select(Diary.id).where(
            Diary.user_id == profile.id,
            Diary.activity_date == activity_date_for(now, profile.timezone) - timedelta(days=1),
            Diary.kind.in_(("shared_day", "capi_day")),
            Diary.record_status == "published",
            Diary.deleted_at.is_(None),
            Diary.published_at >= today_start,
            Diary.published_at <= now,
            Diary.first_read_at.is_(None),
        ).order_by(Diary.published_at.desc(), Diary.id).limit(1)
    )).scalars().first()


async def _deliver_morning(
    session: AsyncSession, profile, now: datetime, stats: dict | None, *,
    column: str, copy, data: dict[str, str], collapse_key: str,
) -> int:
    """아침 푸시 공통 발송. copy = 선점한 뒤 (제목, 본문)을 만드는 함수."""
    tokens = list(dict.fromkeys(t for t in await _tokens(session, profile.id) if t))
    if not tokens:
        return 0
    if session is not None:
        await session.commit()
    try:
        access_token = await push.prepare_access_token()
    except Exception:  # noqa: BLE001  # No message has been submitted, so a later tick can retry.
        _log.warning("아침 푸시 인증 준비 실패 — 발송 슬롯 미소진(%s, user=%s)", column, profile.id)
        _count_push_setup_failure(stats)
        return 0
    if access_token is None:
        return 0
    if not await _claim_send_slot(session, profile, column, now=now):
        return 0
    # The claim commits before network I/O. An ambiguous delivery failure must
    # not release it: FCM may already have accepted a message for another device.
    title, body = copy()
    sent = await push.send(
        tokens, title, body, data=data, expires_at=_expires_at(now, profile.timezone),
        access_token=access_token, collapse_key=collapse_key,
    )
    _account_push(stats, sent)
    await _invalidate_dead_tokens(session, profile.id, getattr(sent, "invalid_tokens", ()), stats=stats)
    if not sent:
        _log.warning("아침 푸시 미전달 — 당일 재발송 생략(%s, user=%s)", column, profile.id)
    return sent


async def notify_morning(
    session: AsyncSession, profile, *, now: datetime | None = None, stats: dict | None = None,
) -> int:
    """현지 10시 아침 일기 푸시."""
    if not settings.morning_push_enabled:
        return 0
    now = now or datetime.now(timezone.utc)
    if _local_hour(now, profile.timezone) != DIARY_MORNING_HOUR:
        return 0  # A delayed/manual invocation must not send a morning push at night.
    if not await _enabled(session, profile.id, "morning_diary"):
        return 0
    diary_id = await _morning_diary_id(session, profile, now)
    if diary_id is None:
        return 0
    return await _deliver_morning(
        session, profile, now, stats, column="morning_notified_at",
        copy=lambda: push_copy.diary(getattr(profile, "language", None)),
        data={"link": "diary", "diary_id": str(diary_id)}, collapse_key="diary",
    )


@dataclass(frozen=True)
class RoutineToday:
    """오늘 할 루틴이 있고 아직 하나도 안 한 사람의 스트릭."""

    streak: int  # 지금 스트릭(오늘은 아직이라 어제까지) — 앱 화면의 연속 기록과 같은 계산
    lost: int    # 오늘 전 마지막 루틴 날에 깨진 스트릭 길이. streak가 0일 때만 0보다 클 수 있다


async def _routine_today(session: AsyncSession, profile, now: datetime) -> RoutineToday | None:
    """오늘(보상일, 현지 0시 경계) 할 루틴이 있고(스킵 제외) 완료가 0개면 스트릭. 아니면 None.

    계산은 앱 기록 화면(routine.stats의 summary)과 같은 routine_records를 쓴다.
    """
    today = reward_date_for(now, profile.timezone)
    rows = await routine._all_routines(session, profile.id)
    if not rows:
        return None
    # 앱은 완료 날짜를 기기 시간대로 저장한다. 서버 기준 오늘보다 뒤 날짜의 완료가 있으면 기기 시간대가
    # 앞서 있다는 뜻이라, 이미 한 사람에게 "아직 안 했다"고 보내지 않도록 건너뛴다.
    latest = (await session.execute(
        select(func.max(RoutineCompletion.activity_date)).where(RoutineCompletion.user_id == profile.id)
    )).scalar()
    if latest is not None and latest > today:
        return None
    records = await routine._routine_records(session, profile, rows, today)
    totals = routine_records.totals(records.values(), today)
    if not totals.scheduled[today] or totals.completed[today]:
        return None
    streak = routine_records.summary(totals, today)["current_streak"]
    lost = 0
    before = [day for day in totals.scheduled if day < today]
    if not streak and before:
        # 오늘 0이면 오늘 전 마지막으로 루틴이 있던 날을 안 해서 깨진 것이다. 그날을 '아직 안 끝난 날'로
        # 놓고 센 스트릭이 깨진 길이다. 매일 루틴이면 어제, 월·수·금 루틴이면 수요일에 월요일 것을 본다.
        lost = routine_records.summary(totals, max(before))["current_streak"]
    return RoutineToday(streak, lost)


async def _routine_today_safe(
    session: AsyncSession, profile, now: datetime, stats: dict | None = None,
) -> RoutineToday | None:
    """판단 실패는 None(보내지 않음) — 루틴 푸시가 다른 푸시를 막지 않게 savepoint로 격리.
    실패 건수는 stats["routine_check_failed"]로 슬랙 요약에 드러낸다."""
    try:
        async with session.begin_nested():
            return await _routine_today(session, profile, now)
    except Exception:  # noqa: BLE001
        _log.warning("루틴 상태 판단 실패 — 루틴 푸시 생략(user=%s)", profile.id, exc_info=True)
        if stats is not None:
            stats["routine_check_failed"] = stats.get("routine_check_failed", 0) + 1
        return None


async def notify_routine_morning(
    session: AsyncSession, profile, *, now: datetime | None = None, stats: dict | None = None,
) -> int:
    """현지 9시 루틴 아침 푸시. 직전 루틴 날에 2일 이상 스트릭이 깨졌으면 rm.break, 아니면 요일 문구."""
    now = now or datetime.now(timezone.utc)
    if _local_hour(now, profile.timezone) != ROUTINE_MORNING_HOUR:
        return 0
    if not await _push_on(session, "routine_morning"):
        return 0
    # 앱의 루틴 알림 스위치는 휴대폰에만 있어 서버가 볼 수 없다 — '저녁 대화' 스위치를 따른다.
    if not await _enabled(session, profile.id, "evening_chat"):
        return 0
    today = await _routine_today_safe(session, profile, now, stats)
    if today is None:
        return 0

    def copy() -> tuple[str, str]:
        lang = getattr(profile, "language", None)
        if today.lost >= 2:  # 1일짜리는 스트릭으로 보지 않는다
            return push_copy.routine(push_copy.ROUTINE_BREAK, lang, lost=today.lost)
        weekday = now.astimezone(safe_zone(profile.timezone)).isoweekday()
        return push_copy.routine(push_copy.WEEKDAYS[weekday - 1], lang)

    return await _deliver_morning(
        session, profile, now, stats, column="routine_notified_at",
        copy=copy, data={"link": "routine"}, collapse_key="routine",
    )


async def _override_copy(session, profile, now: datetime) -> tuple[str, str] | None:
    """app_config 오버라이드가 유저의 활동일과 일치하면 (제목, 본문). 아니면 None.

    조회·파싱 실패는 평소 문구로 발송(fail-open, 이 파일의 규칙) — 공지가 하루 늦는 게
    푸시의 조용한 소멸보다 낫다. 값 검증이 빡빡한 이유: 이 행은 운영자가 손으로 넣는다 —
    언어 누락·문자열 오타가 "일부 유저만 엉뚱한 문구"로 새는 것보다 평소 문구가 낫다.
    """
    try:
        async with session.begin_nested():  # DB 실패 격리 — 세션 오염 방지(이웃 신호 조회 선례)
            cfg = (await get_config_values(session, [OVERRIDE_KEY])).get(OVERRIDE_KEY)
        if not isinstance(cfg, dict):
            return None
        if activity_date_for(now, profile.timezone) != date.fromisoformat(cfg["date"]):
            return None
        table = {k: v for k, v in cfg.items() if k != "date"}
        # 전 언어 버킷이 ["제목","본문"] 형태여야 발송 — 하나라도 누락·오타(문자열이 2글자로
        # 쪼개지는 사고 등)면 전원 평소 문구(fail-open). 일부 언어만 깨진 공지를 내보내지 않는다.
        if any(
            not isinstance(table.get(lang), (list, tuple)) or len(table[lang]) != 2
            for lang in i18n.SUPPORTED
        ):
            return None
        title, body = i18n.pick(table, getattr(profile, "language", None))
        return str(title), str(body)
    except Exception:  # noqa: BLE001
        _log.warning("저녁 오버라이드 해석 실패 — 평소 문구(user=%s)", profile.id, exc_info=True)
        return None


async def _streak_at_risk(session: AsyncSession, profile, now: datetime, stats: dict | None) -> int:
    """저녁 스트릭 위기면 그 스트릭 일수, 아니면 0. 꺼져 있거나 판단에 실패하면 0(대화 푸시로)."""
    if not await _push_on(session, "routine_risk"):
        return 0
    today = await _routine_today_safe(session, profile, now, stats)
    return today.streak if today is not None else 0


async def notify_evening(
    session: AsyncSession, profile, now: datetime | None = None, stats: dict | None = None
) -> int:
    """저녁 20시 푸시 — 운영 공지 → 스트릭 위기 → 캐피 대화 중 처음 맞는 하나. now는 tick이 주입
    (테스트 결정성), stats는 종류별 발송 카운트를 얹을 딕셔너리(관측 — tick의 out, 없으면 생략)."""
    if not await _enabled(session, profile.id, "evening_chat"):
        return 0
    now = now or datetime.now(timezone.utc)
    if _local_hour(now, profile.timezone) != EVENING_HOUR:
        return 0  # 늦게 돈 처리·수동 실행이 밤에 저녁 푸시를 보내지 않게
    # 기기 토큰이 없으면 선점하지 않는다 — 받지도 않은 대화 푸시의 말이 대화창 첫마디로 뜨지 않게.
    tokens = await _tokens(session, profile.id)
    if not tokens:
        return 0
    lang = getattr(profile, "language", None)
    variant = link = None
    collapse_key = "chat"
    override = await _override_copy(session, profile, now)
    streak = 0 if override is not None else await _streak_at_risk(session, profile, now, stats)
    if override is not None:
        category = OVERRIDE_CATEGORY
        title, body = override
    elif streak:
        category, variant, link, collapse_key = ROUTINE_RISK_CATEGORY, push_copy.ROUTINE_RISK, "routine", "routine"
        title, body = push_copy.routine(variant, lang, n=streak)
    else:
        # 하루 대화량을 모두 소진한 유저는 저녁 안부(대화 유도)를 받지 않는다 (SOMA-291).
        # tokens_remaining=None = 무제한 tier → 계속 발송. <=0 = 소진 → 스킵.
        # 오버라이드·스트릭 위기는 대화 유도가 아니라 이 게이트를 건너뛴다.
        from app.services import gating

        g = await gating.resolve(session, str(profile.id), now)
        remaining = g.entitlement.get("tokens_remaining")
        if remaining is not None and remaining <= 0:
            return 0
        ok, last = await _last_chat_at(session, profile)
        # 대화 중이거나 막 가입했으면(첫인사를 보고 있을 때) 선점하지 않고 다음 틱에 다시 본다.
        recent = [t for t in (last if ok else None, getattr(profile, "created_at", None)) if t is not None]
        if any(now - t < ACTIVE_CHAT_WINDOW for t in recent):
            return 0
        category, variant = _evening_variant(profile, g, now, ok, last)
        title, body = push_copy.chat(variant, lang)
        link = "conversation"
    # 발송 슬롯만 먼저 선점한다. FCM 수락 전에는 greeting이 문구를 보지 못해야 한다.
    if not await _claim_send_slot(
        session, profile, "evening_notified_at", now=now, values={"evening_push_variant": None},
    ):
        return 0  # 오늘 이미 발송 — 멱등 스킵
    # 외부 호출(FCM) 전 읽기 트랜잭션 해제 — 커넥션을 쥔 채 네트워크를 기다리지 않는다(#16+#24).
    # (단위 테스트는 조회 전부를 mock하고 session=None을 넘긴다 — 가드.)
    if session is not None:
        await session.commit()
    try:
        sent = await push.send(
            tokens, title, body, data={"link": link} if link else None,
            expires_at=_expires_at(now, profile.timezone), collapse_key=collapse_key,
        )
    except Exception:
        # 인증 준비는 send 안에서 한다. 처리는 그대로(틱이 기록), 전면 장애 판정용으로만 센다.
        _count_push_setup_failure(stats)
        raise
    _account_push(stats, sent)
    if sent and variant is not None:
        await _record_evening_variant(session, profile, now, variant)
    await _invalidate_dead_tokens(session, profile.id, getattr(sent, "invalid_tokens", ()), stats=stats)
    # FCM 수락(sent>0) 뒤에만 센다. 실제 기기 도착을 뜻하지 않는다.
    if sent and stats is not None:
        key = f"evening_{category}"
        stats[key] = stats.get(key, 0) + 1
    return sent


async def _record_evening_variant(
    session: AsyncSession, profile, now: datetime, variant: str,
) -> None:
    """FCM이 한 요청 이상 수락한 뒤 문구를 노출한다. 기기 수신 확인은 아니다.

    기록 실패·프로세스 종료 시 첫마디 연결만 생략되고, 선점은 유지돼 재발송하지 않는다.
    """
    if session is None:
        return
    try:
        await session.execute(
            update(UserDailyStats)
            .where(
                UserDailyStats.user_id == profile.id,
                UserDailyStats.activity_date == activity_date_for(now, profile.timezone),
                UserDailyStats.evening_notified_at.is_not(None),
            )
            .values(evening_push_variant=variant)
        )
        await session.commit()
    except Exception as error:  # noqa: BLE001
        _log.warning("저녁 문구 id 기록 실패 error_type=%s", type(error).__name__)
        await session.rollback()


# ── 저녁 대화 갈래 ──────────────────────────────────────────
# 신호 조회는 savepoint로 격리하고(fail-open, turn_context.py 선례), 실패 시 중립 폴백
# (push_copy.FALLBACK — 어떤 유저 상태에도 거짓이 되지 않는 말)으로 발송한다.


def _category(*, spoke_today: bool, days_since: int | None, days_since_signup: int | None) -> str:
    """갈래 사다리(순수 함수) — 첫 매치 승리.

    days_since=None = 대화 이력 없음 — 가입 초기면 first_touch, 아니면 default_long.
    """
    if spoke_today:
        return push_copy.MORE_CHAT
    if days_since is None:
        if days_since_signup is not None and days_since_signup <= FIRST_TOUCH_MAX_DAYS:
            return push_copy.FIRST_TOUCH
        return push_copy.DEFAULT_LONG
    if days_since <= 2:
        return push_copy.DEFAULT_RECENT
    if days_since <= 6:
        return push_copy.DEFAULT_MISSING
    return push_copy.DEFAULT_LONG


def _evening_variant(profile, g, now: datetime, ok: bool, last: datetime | None) -> tuple[str, str]:
    """(갈래, 문구 id). 마지막 대화 조회가 실패했거나 계산이 실패하면 ('fallback', 중립 문구)."""
    if not ok:
        return "fallback", push_copy.FALLBACK
    try:
        # 마지막 대화로부터의 활동일 차. None = 대화 이력 없음(정상 상태, 실패와 구분).
        days_since = None if last is None else (
            activity_date_for(now, profile.timezone) - activity_date_for(last, profile.timezone)
        ).days
        category = _category(
            spoke_today=g.tokens_used > 0,  # 오늘(활동일) 대화 여부 — gating.resolve가 이미 로드한 값
            days_since=days_since,
            days_since_signup=_days_since_signup(profile, now),
        )
    except Exception:  # noqa: BLE001
        _log.warning("저녁 분기 실패 — 중립 폴백(user=%s)", profile.id, exc_info=True)
        return "fallback", push_copy.FALLBACK
    return category, push_copy.pick(category)


async def _last_chat_at(session, profile) -> tuple[bool, datetime | None]:
    """(조회 성공 여부, 마지막 대화 시각 chat_contexts.last_active_at). 이력 없음 = (True, None)."""
    try:
        async with session.begin_nested():
            last = (
                await session.execute(
                    select(ChatContext.last_active_at).where(ChatContext.user_id == profile.id)
                )
            ).scalars().first()
        return True, last
    except Exception:  # noqa: BLE001
        _log.warning("저녁 분기: last_active 조회 실패 — 중립 폴백(user=%s)", profile.id, exc_info=True)
        return False, None


def _days_since_signup(profile, now: datetime) -> int | None:
    """가입으로부터의 활동일 차. created_at 없거나 계산 실패면 None(→ first_touch 아님)."""
    created = getattr(profile, "created_at", None)
    if created is None:
        return None
    try:
        return (
            activity_date_for(now, profile.timezone)
            - activity_date_for(created, profile.timezone)
        ).days
    except Exception:  # noqa: BLE001
        return None

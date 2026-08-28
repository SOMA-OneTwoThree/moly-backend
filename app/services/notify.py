"""알림 발송 조립 — 설정(기본 on) 확인 → 기기 토큰 로드 → FCM 발송. 워커가 09:00/20:00 호출.

저녁 20시 푸시는 유저 상태로 카테고리를 분기해 문구를 다양화한다(문구 풀은 push_copy.py).
분기 신호는 발송 순간에 읽는다 — 사전 계산·LLM·새 테이블 없음. 모든 날짜 판정은 활동일
(로컬 04시 경계, 시스템 공통 규약) 기준이다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import activity_date_for, current_activity_date
from app.models.chat_context import ChatContext
from app.models.diary import Diary
from app.models.user_daily_stats import UserDailyStats
from app.models.user_device import UserDevice
from app.models.user_notification_settings import UserNotificationSettings
from app.services import i18n, push, push_copy
from app.services.config_store import get_config_values

_log = logging.getLogger("moly-worker")

# 푸시 문구 — 유저 언어별(profile.language). 없거나 미지원 언어면 en 폴백(i18n.pick).
# _EVENING은 분기 신호 조회가 실패했을 때의 **중립 폴백**이기도 하다 — 사실 주장("오랜만" 등)이
# 없어 어떤 유저 상태에도 거짓이 되지 않는다. 폴백을 카테고리 문구로 두면 안 되는 이유다.
_MORNING = {
    "ko": ("캐피", "캐피가 어젯밤 일기를 남겼어요. 몰래 보러가볼까요?"),
    "en": ("Cappy", "Cappy left a diary last night. Want to sneak a peek?"),
    "ja": ("キャピー", "キャピーが昨夜、日記を残したよ。こっそり見に行かない？"),
}
_EVENING = {
    "ko": ("캐피", "오늘 하루는 어땠어? 나랑 같이 얘기하면서 놀자."),
    "en": ("Cappy", "How was your day? Come talk and hang out with me."),
    "ja": ("キャピー", "今日はどんな一日だった？ぼくと一緒におしゃべりしよう。"),
}

# 가입 후 이 일수(활동일 차) 이내 + 대화 이력 없음 = first_touch. 지나면 default_long.
FIRST_TOUCH_MAX_DAYS = 7

# app_config 키 — 저녁 푸시 1회성 오버라이드(공지). value(jsonb) 형식:
# {"date": "YYYY-MM-DD", "ko": ["제목", "본문"], "en": [...], "ja": [...]}
# 유저의 활동일이 date와 같으면 카테고리 분기 대신 이 문구로 발송한다. 운영은 쿼리만으로
# (INSERT=예약 / DELETE=취소, 재배포 없음). 날짜가 지나면 자동으로 평소 동작 복귀.
OVERRIDE_KEY = "evening_push_override"
OVERRIDE_CATEGORY = "override"

# 카테고리별 발송 카운트 키(관측) — tick의 counts 초기화가 이 목록으로 만든다.
# "fallback" = 신호 조회 실패로 중립 문구가 나간 건(분포 이상 감지용).
EVENING_STAT_KEYS: tuple[str, ...] = push_copy.CATEGORIES + ("fallback", OVERRIDE_CATEGORY)


def _push_text(table: dict, language: str | None) -> tuple[str, str]:
    return i18n.pick(table, language)


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


async def _tokens(session: AsyncSession, uid) -> list[str]:
    return list(
        (
            await session.execute(select(UserDevice.push_token).where(UserDevice.user_id == uid))
        ).scalars().all()
    )


async def _claim_send_slot(session: AsyncSession, profile, column: str) -> bool:
    """유저×활동일당 해당 알림을 최초 1회만 '선점'(atomic upsert). 이미 발송했으면 False.

    발송 전에 마커를 선점(at-most-once) — 재시도·중복 실행·15분 케이던스에서 중복 푸시 방지.
    발송 실패 시 그날은 스킵되나(마커 잔존), 스팸보다 낫다(알림은 on/off 베스트에포트).
    활동일(로컬 04:00 경계) 기준 — 아침 09:00·저녁 20:00 모두 당일에 귀속된다.
    """
    ad = current_activity_date(profile.timezone)
    col = getattr(UserDailyStats, column)
    stmt = (
        pg_insert(UserDailyStats)
        .values(user_id=profile.id, activity_date=ad, **{column: func.now()})
        .on_conflict_do_update(
            index_elements=["user_id", "activity_date"],
            set_={column: func.now()},
            where=col.is_(None),  # 이미 발송(NOT NULL)이면 갱신 안 함 → RETURNING 비어 skip
        )
        .returning(UserDailyStats.id)
    )
    claimed = (await session.execute(stmt)).scalars().first() is not None
    await session.commit()
    return claimed


async def notify_morning(session: AsyncSession, profile) -> int:
    # 전역 킬스위치(SOMA-338): 아침 일기 푸시 차단 → 저녁 안부만 발송. 코드·문구는 유지, 플래그로만 막는다.
    if not settings.morning_push_enabled:
        return 0
    if not await _enabled(session, profile.id, "morning_diary"):
        return 0
    if not await _claim_send_slot(session, profile, "morning_notified_at"):
        return 0  # 오늘 이미 발송 — 멱등 스킵
    title, body = _push_text(_MORNING, getattr(profile, "language", None))
    tokens = await _tokens(session, profile.id)
    # 외부 호출(FCM) 전 읽기 트랜잭션 해제 — 커넥션을 쥔 채 네트워크를 기다리지 않는다(#16+#24).
    # (단위 테스트는 조회 전부를 mock하고 session=None을 넘긴다 — 가드.)
    if session is not None:
        await session.commit()
    return await push.send(tokens, title, body)


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


async def notify_evening(
    session: AsyncSession, profile, now: datetime | None = None, stats: dict | None = None
) -> int:
    """저녁 20시 푸시 — 유저 상태 분기 문구. now는 tick이 주입(테스트 결정성), stats는
    카테고리별 발송 카운트를 얹을 딕셔너리(관측 — tick의 out, 없으면 생략)."""
    if not await _enabled(session, profile.id, "evening_chat"):
        return 0
    now = now or datetime.now(timezone.utc)
    override = await _override_copy(session, profile, now)
    g = None  # override 경로에선 미사용 — 분기 조건이 어긋나도 NameError가 안 나게 고정
    if override is None:
        # 하루 대화량을 모두 소진한 유저는 저녁 안부(대화 유도)를 받지 않는다 (SOMA-291).
        # tokens_remaining=None = 무제한 tier → 계속 발송. <=0 = 소진 → 스킵.
        # 오버라이드는 대화 유도가 아니라 공지라 이 게이트를 건너뛴다(소진 유저에게도 발송).
        from app.services import gating

        g = await gating.resolve(session, str(profile.id), now)
        remaining = g.entitlement.get("tokens_remaining")
        if remaining is not None and remaining <= 0:
            return 0
    if not await _claim_send_slot(session, profile, "evening_notified_at"):
        return 0  # 오늘 이미 발송 — 멱등 스킵
    # 클레임은 위에서 이미 커밋됐다 — 이 아래에서 무슨 일이 나도 발송은 포기하지 않는다
    # (포기 = 그날 푸시의 조용한 소멸). 분기 전체를 구조적으로 가드하고 중립 폴백으로 발송.
    if override is not None:
        category = OVERRIDE_CATEGORY
        title, body = override
    else:
        try:
            category, title, body = await _evening_copy(session, profile, g, now)
        except Exception:  # noqa: BLE001
            _log.warning("저녁 분기 실패 — 중립 폴백(user=%s)", profile.id, exc_info=True)
            category = "fallback"
            title, body = _push_text(_EVENING, getattr(profile, "language", None))
    tokens = await _tokens(session, profile.id)
    # 외부 호출(FCM) 전 읽기 트랜잭션 해제 — 커넥션을 쥔 채 네트워크를 기다리지 않는다(#16+#24).
    # (단위 테스트는 조회 전부를 mock하고 session=None을 넘긴다 — 가드.)
    if session is not None:
        await session.commit()
    sent = await push.send(tokens, title, body)
    # stats는 실제 발송(sent>0) 뒤에 — 토큰 없는 유저를 세면 tick의 '저녁 N건'과 분포 합이 어긋난다.
    if sent and stats is not None:
        key = f"evening_{category}"
        stats[key] = stats.get(key, 0) + 1
    return sent


# ── 저녁 문구 분기 ──────────────────────────────────────────
# 클레임이 이미 커밋된 뒤에 돈다 — 여기서 실패해도 발송을 포기하면 안 된다(그날 푸시가
# 조용히 소멸). 신호 조회는 각각 savepoint로 격리하고(fail-open, turn_context.py 선례),
# 실패 시 중립 폴백(_EVENING)으로 발송한다.


def _category(
    *, spoke_today: bool, has_unread_diary: bool,
    days_since: int | None, days_since_signup: int | None,
) -> str:
    """분기 사다리(순수 함수) — 첫 매치 승리.

    more_chat이 diary_teaser보다 먼저다: 매일 대화하는 유저는 매일 일기가 생기므로
    티저를 앞에 두면 그 유저의 푸시가 티저에 갇힌다(일기 미독률 ~75% 실측). 티저는
    "오늘 대화 안 한 유저 + 신선한 어제 일기"라는 좁은 조건일 때 가장 힘이 세다.
    days_since=None = 대화 이력 없음 — 가입 초기면 first_touch, 아니면 default_long.
    """
    if spoke_today:
        return push_copy.MORE_CHAT
    if has_unread_diary:
        return push_copy.DIARY_TEASER
    if days_since is None:
        if days_since_signup is not None and days_since_signup <= FIRST_TOUCH_MAX_DAYS:
            return push_copy.FIRST_TOUCH
        return push_copy.DEFAULT_LONG
    if days_since <= 2:
        return push_copy.DEFAULT_RECENT
    if days_since <= 6:
        return push_copy.DEFAULT_MISSING
    return push_copy.DEFAULT_LONG


async def _evening_copy(session, profile, g, now: datetime) -> tuple[str, str, str]:
    """(카테고리, 제목, 본문). 신호 실패 시 카테고리 'fallback' + 중립 문구."""
    lang = getattr(profile, "language", None)
    # 오늘(활동일) 대화 여부 — gating.resolve가 이미 로드한 값이라 추가 쿼리 0.
    spoke_today = g.tokens_used > 0
    # spoke_today가 참이어도 신호 2개를 항상 조회한다(의도) — short-circuit하면 그 코호트의
    # 신호 장애가 fallback 통계에 안 잡혀 관측이 왜곡된다. 비용은 인덱스 단건 조회 2개.
    has_unread = await _unread_yesterday_diary(session, profile, now)
    ok, days_since = await _days_since_last_chat(session, profile, now)
    if not ok:
        title, body = _push_text(_EVENING, lang)
        return "fallback", title, body
    category = _category(
        spoke_today=spoke_today,
        has_unread_diary=has_unread,
        days_since=days_since,
        days_since_signup=_days_since_signup(profile, now),
    )
    title, body = push_copy.pick(category, lang)
    return category, title, body


async def _unread_yesterday_diary(session, profile, now: datetime) -> bool:
    """어제(활동일) 캐피 일기가 발행됐고 아직 안 읽었는가. 실패 시 False(분기만 죽고 발송은 산다).

    welcome 일기는 kind + activity_date NULL로 이중 배제. legacy tombstone(source='none')은
    published_at이 NULL이라 published_at <= now 조건이 배제한다.
    """
    try:
        async with session.begin_nested():
            yesterday = activity_date_for(now, profile.timezone) - timedelta(days=1)
            row = (
                await session.execute(
                    select(Diary.id).where(
                        Diary.user_id == profile.id,
                        Diary.activity_date == yesterday,
                        Diary.kind.in_(("shared_day", "capi_day")),
                        Diary.record_status == "published",  # 다른 읽기 경로와 동일 조건
                        Diary.deleted_at.is_(None),
                        Diary.published_at <= now,
                        Diary.first_read_at.is_(None),
                    )
                )
            ).scalars().first()
            return row is not None
    except Exception:  # noqa: BLE001  # 분기 신호 실패가 발송을 죽이면 안 됨
        _log.warning("저녁 분기: 일기 미독 조회 실패 — 티저 생략(user=%s)", profile.id, exc_info=True)
        return False


async def _days_since_last_chat(session, profile, now: datetime) -> tuple[bool, int | None]:
    """(조회 성공 여부, 마지막 대화로부터의 활동일 차). 성공+이력 없음 = (True, None).

    실패(False)와 이력 없음(None)을 구분해야 한다 — 이력 없음은 first_touch/default_long으로
    가는 정상 상태고, 실패는 중립 폴백으로 가야 하는 비정상 상태다.
    """
    try:
        async with session.begin_nested():
            last = (
                await session.execute(
                    select(ChatContext.last_active_at).where(ChatContext.user_id == profile.id)
                )
            ).scalars().first()
        if last is None:
            return True, None
        # 활동일 차 계산도 try 안 — 값 사용까지 가드하는 게 이 파일의 규칙(turn_context 선례).
        days = (
            activity_date_for(now, profile.timezone)
            - activity_date_for(last, profile.timezone)
        ).days
        return True, days
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

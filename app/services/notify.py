"""알림 발송 조립 — 설정(기본 on) 확인 → 기기 토큰 로드 → FCM 발송. 워커가 09:00/20:00 호출."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.time_utils import activity_date_for
from app.models.user_daily_stats import UserDailyStats
from app.models.user_device import UserDevice
from app.models.user_notification_settings import UserNotificationSettings
from app.services import i18n, naming, push

# 푸시 문구 — 유저 언어별(profile.language). 없거나 미지원 언어면 en 폴백(i18n.pick).
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


async def _claim_send_slot(
    session: AsyncSession, profile, column: str, now: datetime | None = None
) -> bool:
    """유저×활동일당 해당 알림을 최초 1회만 '선점'(atomic upsert). 이미 발송했으면 False.

    발송 전에 마커를 선점(at-most-once) — 재시도·중복 실행·15분 케이던스에서 중복 푸시 방지.
    발송 실패 시 그날은 스킵되나(마커 잔존), 스팸보다 낫다(알림은 on/off 베스트에포트).
    활동일(로컬 04:00 경계) 기준 — 아침 09:00·저녁 20:00 모두 당일에 귀속된다.
    now = 워커 틱의 기준 시각. 미지정 시 실시간 — run_tick(now=주입) 리허설이 유효하려면
    호출 경로 전체가 같은 now를 관통해야 한다.
    """
    ad = activity_date_for(now or datetime.now(timezone.utc), profile.timezone)
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


async def notify_morning(session: AsyncSession, profile, now: datetime | None = None) -> int:
    # 전역 킬스위치(SOMA-338): 아침 일기 푸시 차단 → 저녁 안부만 발송. 코드·문구는 유지, 플래그로만 막는다.
    if not settings.morning_push_enabled:
        return 0
    if not await _enabled(session, profile.id, "morning_diary"):
        return 0
    if not await _claim_send_slot(session, profile, "morning_notified_at", now):
        return 0  # 오늘 이미 발송 — 멱등 스킵
    title, body = _push_text(_MORNING, getattr(profile, "language", None))
    return await push.send(await _tokens(session, profile.id), title, body)


async def notify_evening(session: AsyncSession, profile, now: datetime | None = None) -> int:
    if not await _enabled(session, profile.id, "evening_chat"):
        return 0
    # 하루 대화량을 모두 소진한 유저는 저녁 안부(대화 유도)를 받지 않는다 (SOMA-291).
    # tokens_remaining=None = 무제한 tier → 계속 발송. <=0 = 소진 → 스킵.
    from app.services import gating

    g = await gating.resolve(session, str(profile.id))
    remaining = g.entitlement.get("tokens_remaining")
    if remaining is not None and remaining <= 0:
        return 0
    if not await _claim_send_slot(session, profile, "evening_notified_at", now):
        return 0  # 오늘 이미 발송 — 멱등 스킵
    title, body = _push_text(_EVENING, getattr(profile, "language", None))
    return await push.send(await _tokens(session, profile.id), title, body)


async def notify_evening_personalized(
    session: AsyncSession, profile, row, now: datetime
) -> int:
    """개인화 저녁 안부 1건. row = push_personalization.PushRow(사전 생성 문구).

    notify_evening과 **같은 게이트를 같은 순서로** 공유한다(알림설정·토큰 소진) — 개인화가
    설정 off·소진 유저에게 새는 우회 경로가 되면 안 된다. claim도 evening_notified_at을
    공유해 디폴트와 상호배제(하루 1회).

    순서 불변식: render → 결정적 필터 재통과 → **그 다음** claim. claim을 먼저 잡으면
    렌더·필터의 실패가 마커만 소모해 그날 저녁 푸시 전체(디폴트 폴백 포함)를 봉쇄한다.
    claim 이후 실패 표면은 push.send뿐 — 기존 디폴트와 동일한 수용 리스크.
    반환 0 = 미발송(claim 미소모면 호출측이 디폴트로 폴백 가능).
    """
    if not await _enabled(session, profile.id, "evening_chat"):
        return 0
    from app.services import gating, push_personalization

    g = await gating.resolve(session, str(profile.id))
    remaining = g.entitlement.get("tokens_remaining")
    if remaining is not None and remaining <= 0:
        return 0  # SOMA-291: 토큰 소진 유저는 대화 유도 안 함(디폴트와 동일 게이트)
    body = naming.render(row.body, getattr(profile, "nickname", None)) or ""
    # 닉네임은 생성 시점 검수를 안 거쳤다(내용 검증 없는 자유 문자열) — render 결과를 다시 검사.
    if not push_personalization.passes_deterministic_filter(body, row.language):
        return 0
    if not await _claim_send_slot(session, profile, "evening_notified_at", now):
        return 0  # 오늘 이미 발송(디폴트 포함) — 멱등 스킵
    title, _ = _push_text(_EVENING, row.language)
    sent = await push.send(await _tokens(session, profile.id), title, body)
    if sent:
        try:
            await push_personalization.mark_sent(session, profile.id, now, profile.timezone)
        except Exception:  # noqa: BLE001  # 통계 실패가 발송 경로를 죽이면 안 됨
            await session.rollback()
    return sent

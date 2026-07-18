"""알림 발송 조립 — 설정(기본 on) 확인 → 기기 토큰 로드 → FCM 발송. 워커가 09:00/20:00 호출."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_device import UserDevice
from app.models.user_notification_settings import UserNotificationSettings
from app.services import push

_MORNING = ("캐피", "캐피가 어젯밤 일기를 남겼어요. 몰래 보러가볼까요?")
_EVENING = ("캐피", "오늘 하루는 어땠어? 나랑 같이 얘기하면서 놀자.")


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


async def notify_morning(session: AsyncSession, profile) -> int:
    if not await _enabled(session, profile.id, "morning_diary"):
        return 0
    return await push.send(await _tokens(session, profile.id), *_MORNING)


async def notify_evening(session: AsyncSession, profile) -> int:
    if not await _enabled(session, profile.id, "evening_chat"):
        return 0
    return await push.send(await _tokens(session, profile.id), *_EVENING)

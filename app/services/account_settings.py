"""계정 부가 — 알림 설정·푸시토큰·로그아웃·탈퇴(API_SPEC §2)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.models.user_device import UserDevice
from app.models.user_notification_settings import UserNotificationSettings
from app.services.account import _uid

_log = logging.getLogger("moly-backend")

# 알림 2종 고정. 행 없으면 기본 on(true).
_NOTIF_TYPES = ("morning_diary", "evening_chat")


async def get_notifications(session: AsyncSession, user_id: str) -> dict[str, bool]:
    rows = await session.execute(
        select(UserNotificationSettings).where(
            UserNotificationSettings.user_id == _uid(user_id)
        )
    )
    stored = {r.type: r.enabled for r in rows.scalars()}
    return {t: stored.get(t, True) for t in _NOTIF_TYPES}


async def patch_notifications(
    session: AsyncSession, user_id: str, req
) -> dict[str, bool]:
    uid = _uid(user_id)
    provided = {"morning_diary": req.morning_diary, "evening_chat": req.evening_chat}
    for type_, value in provided.items():
        if value is None:
            continue
        stmt = pg_insert(UserNotificationSettings).values(
            user_id=uid, type=type_, enabled=value
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["user_id", "type"], set_={"enabled": value}
        )
        await session.execute(stmt)
    await session.commit()
    return await get_notifications(session, user_id)


async def register_push_token(session: AsyncSession, user_id: str, req) -> None:
    uid = _uid(user_id)
    now = datetime.now(timezone.utc)
    # push_token UNIQUE — 다른 계정에 붙어있던 토큰이면 이 유저로 재귀속(기기 이전).
    stmt = pg_insert(UserDevice).values(
        user_id=uid, platform=req.platform, push_token=req.token, last_active_at=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["push_token"],
        set_={"user_id": uid, "platform": req.platform, "last_active_at": now},
    )
    await session.execute(stmt)
    await session.commit()


async def logout_device(session: AsyncSession, user_id: str, push_token: str) -> None:
    # 해당 토큰 + 본인 것만 삭제(멀티기기 안전).
    await session.execute(
        delete(UserDevice).where(
            UserDevice.push_token == push_token,
            UserDevice.user_id == _uid(user_id),
        )
    )
    await session.commit()


async def _delete_supabase_user(user_id: str) -> None:
    """Supabase auth 유저 삭제 → 전 테이블 CASCADE(ERD §3.2). 서비스 롤 필요."""
    if not (settings.supabase_url and settings.supabase_service_role_key):
        raise errors.AppError("INTERNAL", 500, "서버 설정 오류로 탈퇴를 처리할 수 없어요.")
    url = f"{settings.supabase_url}/auth/v1/admin/users/{user_id}"
    key = settings.supabase_service_role_key
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.delete(
            url, headers={"apikey": key, "Authorization": f"Bearer {key}"}
        )
    if r.status_code not in (200, 204):
        _log.error("Supabase 유저 삭제 실패: HTTP %s", r.status_code)
        raise errors.AppError("INTERNAL", 500, "탈퇴 처리에 실패했어요. 잠시 후 다시 시도해 주세요.")


async def _delete_memories(user_id: str) -> None:
    """mem0 장기기억 삭제(FK 밖이라 CASCADE 안 됨, ERD §7).

    TODO(mem0 모듈): mem0.delete_all(user_id) 호출로 교체. 현재 mem0 미연동이라 데이터 없음.
    실패해도 탈퇴는 완료 처리(최종적 정리 — 백그라운드 재시도 대상).
    """
    _log.info("mem0 cleanup pending(모듈 미연동): user=%s", user_id)


async def delete_account(session: AsyncSession, user_id: str) -> None:
    _uid(user_id)  # 형식 검증(비정상 토큰 → 401)
    await _delete_supabase_user(user_id)  # CASCADE로 우리 테이블 전부 삭제
    await _delete_memories(user_id)  # mem0 병행(최종적)

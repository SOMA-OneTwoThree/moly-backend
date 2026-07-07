"""account 서비스 — 온보딩·GET /me·PATCH /me. 프로필은 가입 트리거로 이미 존재(ERD §3.2)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import activity_date_for
from app.models.profile import Profile
from app.models.subscription import Subscription
from app.models.user_daily_stats import UserDailyStats
from app.models.user_equipment import UserEquipment
from app.services.entitlement import derive_entitlement
from app.services.limits import effective_token_config


def _validate_timezone(tz_name: str) -> None:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise errors.validation("유효하지 않은 타임존이에요.", {"timezone": tz_name}) from e


def _uid(user_id: str) -> uuid.UUID:
    """JWT sub → UUID. 형식 오류면 500이 아니라 401(비정상 토큰)."""
    try:
        return uuid.UUID(user_id)
    except ValueError as e:
        raise errors.unauthorized() from e


async def _load_profile(session: AsyncSession, user_id: str) -> Profile:
    profile = await session.get(Profile, _uid(user_id))
    if profile is None:
        raise errors.AppError("NOT_FOUND", 404, "프로필을 찾을 수 없어요.")
    return profile


async def _load_active_subscription(
    session: AsyncSession, user_id: str, now: datetime
) -> Subscription | None:
    rows = await session.execute(
        select(Subscription)
        .where(
            Subscription.user_id == _uid(user_id),
            Subscription.status.in_(["active", "grace_period"]),
            Subscription.expires_at > now,
        )
        .order_by(Subscription.expires_at.desc())
        .limit(1)
    )
    return rows.scalars().first()


async def _load_tokens_used(session: AsyncSession, user_id: str, activity_date: date) -> int:
    rows = await session.execute(
        select(UserDailyStats.tokens_used).where(
            UserDailyStats.user_id == _uid(user_id),
            UserDailyStats.activity_date == activity_date,
        )
    )
    used = rows.scalars().first()
    return used or 0


async def _load_equipment(session: AsyncSession, user_id: str) -> dict[str, str]:
    rows = await session.execute(
        select(UserEquipment).where(UserEquipment.user_id == _uid(user_id))
    )
    return {row.slot: str(row.shop_item_id) for row in rows.scalars()}


async def _build_entitlement(
    session: AsyncSession, profile: Profile, now: datetime
) -> dict[str, Any]:
    activity_date = activity_date_for(now, profile.timezone)
    sub = await _load_active_subscription(session, str(profile.id), now)
    tokens_used = await _load_tokens_used(session, str(profile.id), activity_date)
    # app_config 값 우선, 없으면 임의 기본값(settings) — /me와 /chat 한도 일관.
    config = await effective_token_config(session)
    return derive_entitlement(profile, sub, tokens_used, config, now)


def _profile_block(profile: Profile) -> dict[str, Any]:
    return {
        "nickname": profile.nickname,
        "timezone": profile.timezone,
        "language": profile.language,
        "onboarded": profile.nickname is not None,
    }


def assemble_me(
    profile: Profile, entitlement: dict[str, Any], equipment: dict[str, str]
) -> dict[str, Any]:
    """부팅 집계 조립(순수). equipment = {slot: shop_item_id}."""
    return {
        "profile": _profile_block(profile),
        "entitlement": entitlement,
        "wallet": {"balance": profile.hay_balance},
        "equipment": {
            "background_id": equipment.get("background"),
            "head_id": equipment.get("head"),
            "neck_id": equipment.get("neck"),
            "body_id": equipment.get("body"),
        },
    }


async def get_me(session: AsyncSession, user_id: str) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    now = datetime.now(timezone.utc)
    entitlement = await _build_entitlement(session, profile, now)
    equipment = await _load_equipment(session, user_id)
    return assemble_me(profile, entitlement, equipment)


async def onboarding(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    _validate_timezone(req.timezone)
    profile = await _load_profile(session, user_id)
    # 온보딩은 1회만 — 이미 완료(nickname 존재)면 거부(재시도·중복탭·위조 방어).
    if profile.nickname is not None:
        raise errors.already_onboarded()
    profile.nickname = req.nickname
    profile.timezone = req.timezone
    profile.language = req.language
    await session.commit()
    now = datetime.now(timezone.utc)
    entitlement = await _build_entitlement(session, profile, now)
    return {"profile": _profile_block(profile), "entitlement": entitlement}


async def patch_me(session: AsyncSession, user_id: str, req) -> dict[str, Any]:
    if req.timezone is not None:
        _validate_timezone(req.timezone)
    profile = await _load_profile(session, user_id)
    if req.nickname is not None:
        profile.nickname = req.nickname
    if req.language is not None:
        profile.language = req.language
    if req.timezone is not None:
        profile.timezone = req.timezone
    await session.commit()
    return {"profile": _profile_block(profile)}

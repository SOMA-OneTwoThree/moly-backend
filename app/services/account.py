"""계정 공유 헬퍼 — 프로필·구독·사용량 로드(도메인 서비스 공용).

계정 API 자체(온보딩·/me·알림·푸시토큰·로그아웃·탈퇴)는 moly-auth 서버로
이관됨(2026-07-09) — 여기는 chat/shop/gating 등이 쓰는 로드 헬퍼만 남는다.
프로필은 가입 트리거로 생성(ERD §3.2), 계정 쓰기는 moly-auth가 담당.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.profile import Profile
from app.models.subscription import Subscription
from app.models.user_daily_stats import UserDailyStats


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

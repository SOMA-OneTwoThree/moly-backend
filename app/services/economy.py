"""건초·충전소 — 지갑 조회·원장 내역·출석/루틴 보상(서버 권위·멱등)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import activity_date_for
from app.models.hay_pack import HayPack
from app.models.hay_transaction import HayTransaction
from app.models.routine import RoutineCompletion
from app.models.user_daily_stats import UserDailyStats
from app.services import hay_ledger
from app.services.account import _load_profile, _uid

HAY_ATTENDANCE = 10
HAY_ROUTINE_REWARD = 10
HAY_AD = 10
AD_DAILY_LIMIT = 10
ROUTINE_PAIR_REQUIRED = 2


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def get_wallet(session: AsyncSession, user_id: str) -> dict[str, int]:
    profile = await _load_profile(session, user_id)
    return {"balance": profile.hay_balance}


async def list_transactions(
    session: AsyncSession, user_id: str, *, limit: int = 30, cursor: str | None = None
) -> dict[str, Any]:
    uid = _uid(user_id)
    limit = max(1, min(limit, 100))
    q = select(HayTransaction).where(HayTransaction.user_id == uid)
    if cursor:
        q = q.where(HayTransaction.id < int(cursor))
    q = q.order_by(HayTransaction.id.desc()).limit(limit + 1)
    rows = list((await session.execute(q)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    data = [
        {
            "id": str(t.id), "type": t.type, "amount": t.amount,
            "balance_after": t.balance_after, "created_at": _iso(t.created_at),
        }
        for t in rows
    ]
    return {"data": data, "next_cursor": str(rows[-1].id) if (has_more and rows) else None}


async def _daily(session: AsyncSession, uid: uuid.UUID, activity_date) -> UserDailyStats:
    """user_daily_stats 행 get-or-create(행 잠금). 당일 첫 동시요청 레이스 방지:
    먼저 upsert(없으면 삽입, 있으면 무시)로 행을 보장한 뒤 FOR UPDATE로 잠금 조회."""
    await session.execute(
        pg_insert(UserDailyStats)
        .values(user_id=uid, activity_date=activity_date)
        .on_conflict_do_nothing(index_elements=["user_id", "activity_date"])
    )
    return (
        await session.execute(
            select(UserDailyStats)
            .where(UserDailyStats.user_id == uid, UserDailyStats.activity_date == activity_date)
            .with_for_update()
        )
    ).scalars().first()


async def _routine_completions_today(session: AsyncSession, uid: uuid.UUID, activity_date) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(RoutineCompletion)
            .where(RoutineCompletion.user_id == uid, RoutineCompletion.activity_date == activity_date)
        )
    ).scalar() or 0


async def get_charging_status(session: AsyncSession, user_id: str) -> dict[str, Any]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
    ad = activity_date_for(datetime.now(timezone.utc), profile.timezone)
    stats = (
        await session.execute(
            select(UserDailyStats).where(
                UserDailyStats.user_id == uid, UserDailyStats.activity_date == ad
            )
        )
    ).scalars().first()
    ad_used = stats.ad_reward_count if stats else 0
    attendance_claimed = stats is not None and stats.attendance_claimed_at is not None
    routine_claimed = stats is not None and stats.routine_reward_claimed_at is not None
    done = await _routine_completions_today(session, uid, ad)
    packs = list(
        (
            await session.execute(
                select(HayPack).where(HayPack.is_active.is_(True)).order_by(HayPack.sort_order)
            )
        ).scalars().all()
    )
    return {
        "activity_date": ad.isoformat(),
        "attendance": {"claimable": not attendance_claimed, "reward": HAY_ATTENDANCE},
        "ad": {"views_used": ad_used, "views_limit": AD_DAILY_LIMIT, "reward_per_view": HAY_AD},
        "routine_pair": {
            "completed_today": done, "required": ROUTINE_PAIR_REQUIRED,
            "claimable": done >= ROUTINE_PAIR_REQUIRED and not routine_claimed,
            "reward": HAY_ROUTINE_REWARD,
        },
        "hay_packs": [
            {"product_id": p.app_store_product_id, "amount": p.hay_amount} for p in packs
        ],
        "balance": profile.hay_balance,
    }


async def claim_attendance(session: AsyncSession, user_id: str) -> dict[str, int]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
    ad = activity_date_for(datetime.now(timezone.utc), profile.timezone)
    stats = await _daily(session, uid, ad)
    if stats.attendance_claimed_at is not None:
        raise errors.already_claimed()
    stats.attendance_claimed_at = datetime.now(timezone.utc)
    balance = await hay_ledger.apply(session, uid, "attendance", HAY_ATTENDANCE)
    await session.commit()
    return {"granted": HAY_ATTENDANCE, "balance_after": balance}


async def claim_routine_reward(session: AsyncSession, user_id: str) -> dict[str, int]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
    ad = activity_date_for(datetime.now(timezone.utc), profile.timezone)
    if await _routine_completions_today(session, uid, ad) < ROUTINE_PAIR_REQUIRED:
        raise errors.routine_goal_not_met()
    stats = await _daily(session, uid, ad)
    if stats.routine_reward_claimed_at is not None:
        raise errors.already_claimed()
    stats.routine_reward_claimed_at = datetime.now(timezone.utc)
    balance = await hay_ledger.apply(session, uid, "routine_reward", HAY_ROUTINE_REWARD)
    await session.commit()
    return {"granted": HAY_ROUTINE_REWARD, "balance_after": balance}

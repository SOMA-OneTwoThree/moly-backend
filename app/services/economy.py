"""건초·충전소 — 지갑 조회·원장 내역·출석/루틴 보상(서버 권위·멱등)."""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.core.time_utils import current_reward_date
from app.models.hay_transaction import HayTransaction
from app.models.product import Product
from app.models.reward_ad_session import RewardAdSession
from app.models.routine import RoutineCompletion
from app.models.user_daily_stats import UserDailyStats
from app.services import hay_ledger
from app.services.account import _load_profile, _uid

_log = logging.getLogger("moly-backend")
HAY_ATTENDANCE = 10
HAY_ROUTINE_REWARD = 10
HAY_AD = 10
AD_DAILY_LIMIT = 10
ROUTINE_PAIR_REQUIRED = 2


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


async def get_wallet(session: AsyncSession, user_id: str) -> dict[str, int]:
    profile = await _load_profile(session, user_id)
    # 잔액 음수(부채)는 서버 회계 진실이나 클라 계약은 0 하한(음수 노출 안 함, SOMA-372).
    return {"balance": max(0, profile.hay_balance)}


async def list_transactions(
    session: AsyncSession, user_id: str, *, limit: int = 30, cursor: str | None = None
) -> dict[str, Any]:
    uid = _uid(user_id)
    limit = max(1, min(limit, 100))
    q = select(HayTransaction).where(HayTransaction.user_id == uid)
    if cursor:
        try:
            cursor_id = int(cursor)
        except ValueError as e:
            raise errors.validation("잘못된 커서 형식이에요.") from e
        q = q.where(HayTransaction.id < cursor_id)
    q = q.order_by(HayTransaction.id.desc()).limit(limit + 1)
    rows = list((await session.execute(q)).scalars().all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    data = [
        {
            "id": str(t.id), "type": t.type, "amount": t.amount,
            "balance_after": max(0, t.balance_after), "created_at": _iso(t.created_at),
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
    ad = current_reward_date(profile.timezone)
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
                select(Product)
                .where(Product.product_type == "hay_pack", Product.is_active.is_(True))
                .order_by(Product.sort_order)
            )
        ).scalars().all()
    )
    return {
        "activity_date": ad.isoformat(),
        "attendance": {
            "claimable": not attendance_claimed, "claimed": attendance_claimed,
            "reward": HAY_ATTENDANCE,
        },
        "ad": {"views_used": ad_used, "views_limit": AD_DAILY_LIMIT, "reward_per_view": HAY_AD},
        "routine_pair": {
            "completed_today": done, "required": ROUTINE_PAIR_REQUIRED,
            "claimable": done >= ROUTINE_PAIR_REQUIRED and not routine_claimed,
            "claimed": routine_claimed,  # 당일 수령 여부 — 체크 해제해도 재수령 막음
            "reward": HAY_ROUTINE_REWARD,
        },
        "hay_products": [
            {
                "product_id": p.app_store_product_id,
                "play_store_product_id": p.play_store_product_id,
                "amount": p.hay_amount,
            }
            for p in packs
        ],
        "balance": max(0, profile.hay_balance),  # 부채(음수)는 0 하한 노출(SOMA-372)
    }


# --- 단조 보상 윈도우(SOMA-375: tz 변경 재수령 차단) ---
async def _reward_cursor(session: AsyncSession, uid: uuid.UUID) -> date | None:
    """유저가 실제로 보상을 받은 최대 reward_date(없으면 None). tz 역행 재수령 차단용 단조 커서.

    세 보상 종류 통합(union): user_daily_stats(출석·루틴·광고카운트) ∪ reward_ad_sessions(granted).
    per-type가 아니라 union이라야 공격자가 날짜를 되돌리며 보상 종류를 번갈아 받는 걸 막는다.
    광고는 granted 시 ad_reward_count도 오르지만, 과거 데이터 불일치 대비 세션 날짜도 보수적으로 포함.
    """
    stats_max = (
        await session.execute(
            select(func.max(UserDailyStats.activity_date)).where(
                UserDailyStats.user_id == uid,
                or_(
                    UserDailyStats.attendance_claimed_at.isnot(None),
                    UserDailyStats.routine_reward_claimed_at.isnot(None),
                    UserDailyStats.ad_reward_count > 0,
                ),
            )
        )
    ).scalar()
    ad_max = (
        await session.execute(
            select(func.max(RewardAdSession.activity_date)).where(
                RewardAdSession.user_id == uid, RewardAdSession.granted.is_(True)
            )
        )
    ).scalar()
    cands = [d for d in (stats_max, ad_max) if d is not None]
    return max(cands) if cands else None


async def _reward_window_regressed(
    session: AsyncSession, uid: uuid.UUID, reward_date: date
) -> bool:
    """reward_date가 유저 단조 커서보다 과거면 True(tz 역행 재수령 시도). ==는 허용(당일 다종 보상).

    호출측은 반드시 이 조회 **전에** advisory_xact_lock을 쥐고 커밋까지 유지해야 한다 — 그래야
    서로 다른 reward_date 동시요청이 같은 옛 커서를 읽고 각자 통과하는 레이스를 막는다.
    """
    cursor = await _reward_cursor(session, uid)
    if cursor is not None and reward_date < cursor:
        _log.warning(
            "보상 윈도우 역행(tz 악용 의심) user=%s reward_date=%s < cursor=%s",
            uid, reward_date, cursor,
        )
        return True
    return False


async def claim_attendance(session: AsyncSession, user_id: str) -> dict[str, int]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
    # 순서 불변식: lock → 커서 assert → _daily → mutation → commit(SOMA-375 동시성).
    await advisory_xact_lock(session, uid)
    ad = current_reward_date(profile.timezone)
    if await _reward_window_regressed(session, uid, ad):
        raise errors.already_claimed()  # tz 역행 — 과거 날짜 재수령 차단
    stats = await _daily(session, uid, ad)
    if stats.attendance_claimed_at is not None:
        raise errors.already_claimed()
    stats.attendance_claimed_at = datetime.now(timezone.utc)
    tx = await hay_ledger.apply(session, uid, "attendance", HAY_ATTENDANCE)
    await session.commit()
    return {"granted": HAY_ATTENDANCE, "balance_after": max(0, tx.balance_after)}


async def claim_routine_reward(session: AsyncSession, user_id: str) -> dict[str, int]:
    profile = await _load_profile(session, user_id)
    uid = profile.id
    await advisory_xact_lock(session, uid)  # lock → 커서 assert → _daily → mutation
    ad = current_reward_date(profile.timezone)
    if await _reward_window_regressed(session, uid, ad):
        raise errors.already_claimed()  # tz 역행 차단
    if await _routine_completions_today(session, uid, ad) < ROUTINE_PAIR_REQUIRED:
        raise errors.routine_goal_not_met()
    stats = await _daily(session, uid, ad)
    if stats.routine_reward_claimed_at is not None:
        raise errors.already_claimed()
    stats.routine_reward_claimed_at = datetime.now(timezone.utc)
    tx = await hay_ledger.apply(session, uid, "routine_reward", HAY_ROUTINE_REWARD)
    await session.commit()
    return {"granted": HAY_ROUTINE_REWARD, "balance_after": max(0, tx.balance_after)}

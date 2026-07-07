"""건초 원장 — 지급(+)/차감(−)의 단일 지점. 잔액 캐시(profiles) 갱신 + 원장 기록(원자).

동시성: profiles 행 잠금(with_for_update)으로 잔액 레이스 방지. 커밋은 호출측(트랜잭션 조립).
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.models.hay_transaction import HayTransaction
from app.models.profile import Profile


async def apply(
    session: AsyncSession,
    user_id: uuid.UUID,
    tx_type: str,
    amount: int,
    *,
    ref_id: str | None = None,
) -> int:
    """건초 이동. amount>0 지급 / <0 차감. 차감이 잔액 초과면 402. balance_after 반환."""
    profile = await session.get(Profile, user_id, with_for_update=True)
    if profile is None:
        raise errors.AppError("NOT_FOUND", 404, "프로필을 찾을 수 없어요.")
    new_balance = profile.hay_balance + amount
    if new_balance < 0:
        raise errors.insufficient_hay(required=-amount, balance=profile.hay_balance)
    profile.hay_balance = new_balance
    session.add(
        HayTransaction(
            user_id=user_id, type=tx_type, amount=amount,
            balance_after=new_balance, ref_id=ref_id,
        )
    )
    return new_balance

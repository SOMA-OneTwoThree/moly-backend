"""광고 보상 — SSV 확정 기록(멱등) + 수령(일 10회). 시청 확정은 서버-서버 SSV만."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import activity_date_for
from app.models.ad_reward import AdReward
from app.services import economy, hay_ledger
from app.services.account import _load_profile, _uid

AD_REWARD = economy.HAY_AD
AD_DAILY_LIMIT = economy.AD_DAILY_LIMIT


async def record_ssv(session: AsyncSession, user_id: str, ssv_transaction_id: str) -> None:
    """SSV 콜백(서명검증 후) → 확정 레코드 삽입(멱등). custom_data=user_id."""
    profile = await _load_profile(session, user_id)
    ad = activity_date_for(datetime.now(timezone.utc), profile.timezone)
    stmt = pg_insert(AdReward).values(
        ssv_transaction_id=ssv_transaction_id, user_id=profile.id, activity_date=ad, granted=False
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["ssv_transaction_id"])
    await session.execute(stmt)
    await session.commit()


async def claim(session: AsyncSession, user_id: str, ssv_transaction_id: str) -> dict[str, Any]:
    uid = _uid(user_id)
    rec = await session.get(AdReward, ssv_transaction_id, with_for_update=True)  # 동시 클레임 이중지급 방지
    if rec is None or rec.user_id != uid:
        raise errors.ad_verify_failed()  # 422 — 확정 레코드 없음
    if rec.granted:
        raise errors.already_processed()  # 409 — 중복 클레임
    stats = await economy._daily(session, uid, rec.activity_date)
    if stats.ad_reward_count >= AD_DAILY_LIMIT:
        raise errors.ad_limit_reached()  # 429
    stats.ad_reward_count += 1
    rec.granted = True
    balance = await hay_ledger.apply(session, uid, "ad_reward", AD_REWARD, ref_id=ssv_transaction_id)
    await session.commit()
    return {
        "granted": AD_REWARD, "balance_after": balance,
        "views_used": stats.ad_reward_count, "views_limit": AD_DAILY_LIMIT,
    }

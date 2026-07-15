"""광고 보상 — 세션 발급(한도 확인) + SSV 자동 지급. 시청 확정은 서버-서버 SSV만.

클라는 지급을 '수령'하지 않는다(광고 시청 증거 = 서버가 받는 SSV뿐) — SSV 콜백이 바로 지급.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import errors
from app.core.time_utils import current_reward_date
from app.models.reward_ad_session import RewardAdSession
from app.services import economy, hay_ledger
from app.services.account import _load_profile

_log = logging.getLogger("moly-backend")
AD_REWARD = economy.HAY_AD
AD_DAILY_LIMIT = economy.AD_DAILY_LIMIT


async def create_session(session: AsyncSession, user_id: str) -> dict[str, Any]:
    """광고 시청 전 세션 발급. 오늘 한도 초과면 429. 반환 = 클라가 SSV에 실을 값 + 잔여."""
    profile = await _load_profile(session, user_id)
    ad = current_reward_date(profile.timezone)
    stats = await economy._daily(session, profile.id, ad)
    if stats.ad_reward_count >= AD_DAILY_LIMIT:
        raise errors.ad_limit_reached()  # 429
    row = RewardAdSession(user_id=profile.id, activity_date=ad)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {
        "reward_session_id": str(row.session_id),   # SSV custom_data 에 실음
        "admob_user_id": str(profile.id),           # SSV userIdentifier 에 실음
        "views_used": stats.ad_reward_count,
        "views_limit": AD_DAILY_LIMIT,
    }


async def grant_from_ssv(session: AsyncSession, session_id: str, transaction_id: str) -> None:
    """SSV 콜백(서명검증 후) → 세션 조회 후 +10 지급.

    멱등 = 세션당 1회(`granted` 행잠금) + `ssv_transaction_id` UNIQUE(재전송/다른세션 방어).
    일 한도는 지급 시 원자 카운트 체크 — 세션 남발해도 10회 초과 지급 안 됨.
    custom_data = reward_session_id(서명검증된 값) → 세션 소유자에게만 지급.
    """
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        _log.warning("SSV: reward_session_id 형식 오류(%r) — 스킵", session_id)
        return
    row = await session.get(RewardAdSession, sid, with_for_update=True)  # 동시/재전송 직렬화
    if row is None:
        _log.warning("SSV: 세션 없음(%s) — 스킵", session_id)
        return
    if row.granted:
        return  # 이미 지급 — 재전송/중복 콜백 멱등
    stats = await economy._daily(session, row.user_id, row.activity_date)
    if stats.ad_reward_count >= AD_DAILY_LIMIT:
        _log.info("SSV: 일 한도 초과(user=%s) — 미지급", row.user_id)
        return  # 세션은 발급됐어도 실제 지급은 한도로 차단(당일 초과분 미지급)
    stats.ad_reward_count += 1
    row.granted = True
    row.ssv_transaction_id = transaction_id
    # 원장 연결 불필요 — SSV 멱등·추적은 reward_ad_sessions(ssv_transaction_id UNIQUE)가 담당
    await hay_ledger.apply(session, row.user_id, "ad_reward", AD_REWARD)
    try:
        await session.commit()
    except IntegrityError:
        # 같은 transaction_id가 다른 세션으로 이미 지급됨(UNIQUE 충돌) — 롤백, 멱등.
        await session.rollback()

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
from app.core.advisory_lock import advisory_xact_lock
from app.core.pg import unique_violation
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
    # 순서 불변식: lock → 커서 assert → _daily(SOMA-375). tz 역행으로 과거 날짜 세션을 선발급해
    # 나중에 SSV로 수령하는 우회를 발급 시점에 1차 차단(2차는 grant_from_ssv).
    await advisory_xact_lock(session, profile.id)
    ad = current_reward_date(profile.timezone)
    if await economy._reward_window_regressed(session, profile.id, ad):
        raise errors.already_claimed()  # 409 — tz 역행(과거 날짜)
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


async def grant_from_ssv(session: AsyncSession, session_id: str, transaction_id: str) -> str:
    """SSV 콜백(서명검증 후) → 세션 조회 후 +20 지급. 반환 = 처리 결과(콜백 응답 body에 노출).

    결과: granted / invalid_session / session_not_found / duplicate / daily_limit
    / stale_reward_window / transaction_conflict. Google은 body를 보지 않으므로 HTTP는 항상 200
    — 비-200은 Google 재전송을 유발하므로 tz 역행도 예외(409) 아닌 결과값으로 200 유지.
    멱등 = 세션당 1회(`granted` 행잠금) + `ssv_transaction_id` UNIQUE(재전송/다른세션 방어).
    일 한도는 지급 시 원자 카운트 체크 — 세션 남발해도 5회 초과 지급 안 됨.
    custom_data = reward_session_id(서명검증된 값) → 세션 소유자에게만 지급.

    락 순서: 세션을 무잠금 조회해 소유자 uid 확보 → user advisory lock(보상 공통 도메인) →
    세션을 FOR UPDATE + populate_existing으로 재조회. populate_existing이 없으면 무잠금 조회가
    identity map에 남긴 stale granted=False를 재조회가 갱신 못 해, 대기 중 다른 SSV가 지급했어도
    이중 지급될 수 있다(SOMA-375 C11).
    """
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, TypeError):
        _log.warning("SSV: reward_session_id 형식 오류(%r) — 스킵", session_id)
        return "invalid_session"
    pre = await session.get(RewardAdSession, sid)  # 무잠금 — 소유자 uid만 확보(락 순서: user락 먼저)
    if pre is None:
        _log.warning("SSV: 세션 없음(%s) — 스킵", session_id)
        return "session_not_found"
    await advisory_xact_lock(session, pre.user_id)  # 보상 경로 공통 직렬화(커서 조회~지급 원자화)
    # 락 하에서 강제 fresh-load 재조회 — stale granted 이중지급 창 차단(populate_existing 필수)
    row = await session.get(
        RewardAdSession, sid, with_for_update=True, populate_existing=True
    )
    if row is None:
        return "session_not_found"
    if row.granted:
        return "duplicate"  # 이미 지급 — 재전송/중복 콜백 멱등
    # 선발급된 과거 세션(현재 커서보다 뒤)은 지급 거부 — 200 유지(예외 던지지 않음)
    if await economy._reward_window_regressed(session, row.user_id, row.activity_date):
        _log.info("SSV: 보상 윈도우 역행(user=%s date=%s) — 미지급", row.user_id, row.activity_date)
        return "stale_reward_window"
    stats = await economy._daily(session, row.user_id, row.activity_date)
    if stats.ad_reward_count >= AD_DAILY_LIMIT:
        _log.info("SSV: 일 한도 초과(user=%s) — 미지급", row.user_id)
        return "daily_limit"  # 세션은 발급됐어도 실제 지급은 한도로 차단(당일 초과분 미지급)
    stats.ad_reward_count += 1
    row.granted = True
    row.ssv_transaction_id = transaction_id
    # 원장 연결 불필요 — SSV 멱등·추적은 reward_ad_sessions(ssv_transaction_id UNIQUE)가 담당
    try:
        # apply 내부 flush 시점에 ssv_transaction_id UNIQUE 충돌이 먼저 터질 수 있다
        await hay_ledger.apply(session, row.user_id, "ad_reward", AD_REWARD)
        await session.commit()
    except IntegrityError as exc:
        # 같은 transaction_id가 다른 세션으로 이미 지급됨(UNIQUE 충돌) — 롤백, 멱등.
        await session.rollback()
        if unique_violation(exc, "reward_ad_sessions_ssv_transaction_id_key"):
            return "transaction_conflict"
        raise  # 예상 밖 IntegrityError(NULL/FK 등) → 전파(500, 은폐 금지)
    return "granted"

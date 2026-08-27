"""운세 전용 AdMob SSV 적용. 건초 원장과 일일 광고 횟수는 건드리지 않는다."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.core.pg import unique_violation
from app.core.time_utils import reward_date_for
from app.models.fortune import DailyFortune, FortuneAdSession, FortuneProfile
from app.services import fortune, privacy
from app.services.account import _load_profile

_log = logging.getLogger("moly-backend")

_FORTUNE_AD_SESSIONS_REGCLASS = text(
    "SELECT to_regclass('public.fortune_ad_sessions')"
)
_DELETE_EXPIRED_SESSIONS = text("""
WITH candidates AS (
  SELECT session_id FROM fortune_ad_sessions
  WHERE expires_at < now() - interval '7 days'
  ORDER BY expires_at, session_id
  FOR UPDATE SKIP LOCKED
  LIMIT 500
)
DELETE FROM fortune_ad_sessions target
USING candidates
WHERE target.session_id = candidates.session_id
RETURNING target.session_id
""")


def _allowed_ad_units() -> set[str]:
    return {value.strip() for value in settings.fortune_ad_unit_ids.split(",") if value.strip()}


async def verify_from_ssv(
    session: AsyncSession,
    *,
    custom_data: str,
    transaction_id: str,
    signed_user_id: str | None,
    ad_unit: str | None,
    reward_item: str | None,
    reward_amount: str | None,
    now_utc: datetime | None = None,
) -> str:
    """서명 검증이 끝난 운세 SSV를 세션에 멱등 반영한다.

    콜백은 Google 재시도를 유발하지 않도록 모든 영구 거절을 결과 문자열로 반환한다.
    """
    if not custom_data.startswith("fortune:"):
        return "invalid_session"
    try:
        sid = uuid.UUID(custom_data.removeprefix("fortune:"))
        signed_uid = uuid.UUID(signed_user_id or "")
    except (ValueError, TypeError):
        return "invalid_session"
    allowed = _allowed_ad_units()
    if not allowed or ad_unit not in allowed:
        return "invalid_placement"
    if reward_item != settings.fortune_ad_reward_item:
        return "invalid_reward"
    try:
        amount = int(reward_amount or "")
    except ValueError:
        return "invalid_reward"
    if amount != settings.fortune_ad_reward_amount:
        return "invalid_reward"

    pre = await session.get(FortuneAdSession, sid)
    if pre is None:
        return "session_not_found"
    if pre.user_id != signed_uid:
        return "owner_mismatch"
    await advisory_xact_lock(session, pre.user_id)
    row = await session.get(
        FortuneAdSession, sid, with_for_update=True, populate_existing=True
    )
    if row is None:
        return "session_not_found"
    if row.verified:
        return "duplicate" if row.ssv_transaction_id == transaction_id else "session_used"
    now = now_utc or datetime.now(timezone.utc)
    if row.expires_at <= now:
        return "expired"
    try:
        await privacy.ensure_subject_active(session, row.user_id)
    except errors.AppError as exc:  # ACCOUNT_DELETING은 Google에 200으로 종결한다.
        if exc.code == "ACCOUNT_DELETING":
            return "subject_deleting"
        raise

    profile = await session.get(FortuneProfile, row.user_id)
    daily = await session.get(DailyFortune, row.user_id, with_for_update=True)
    account = await _load_profile(session, str(row.user_id))
    today = reward_date_for(now, account.timezone)
    if (
        profile is None
        or daily is None
        or row.fortune_date != today
        or daily.fortune_date != today
        or daily.unlock_state != "locked"
    ):
        return "stale_session"

    row.verified = True
    row.ssv_transaction_id = transaction_id
    row.verified_at = now
    daily.unlock_state = "unlocked"
    daily.unlock_source = "rewarded_ad"
    daily.unlocked_at = now
    # 프로필 수정과 광고 시청이 겹치면 권한만 보존한다. 새 snapshot은 다음 reveal에서 만든다.
    daily.revealed_at = (
        now
        if fortune._current_row(
            daily,
            today=today,
            timezone_name=account.timezone,
            revision=profile.revision,
        )
        else None
    )
    daily.updated_at = now
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if unique_violation(exc, "fortune_ad_sessions_ssv_transaction_id_key"):
            return "transaction_conflict"
        raise
    _log.info("운세 SSV 검증 완료(user=%s date=%s)", row.user_id, row.fortune_date)
    return "verified"


async def cleanup_expired_sessions(session: AsyncSession) -> int:
    """테이블이 적용된 환경에서만 7일이 지난 광고 세션을 bounded 정리한다."""

    table = (await session.execute(_FORTUNE_AD_SESSIONS_REGCLASS)).scalar_one_or_none()
    if table is None:
        # DB migration 전 코드 선배포도 안전하다. 기능 flag와 retention 생명주기는 분리한다.
        await session.commit()
        return 0
    rows = (await session.execute(_DELETE_EXPIRED_SESSIONS)).all()
    await session.commit()
    return len(rows)

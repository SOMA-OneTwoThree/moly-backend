"""오늘의 운세 핵심 서비스 — 프로필, 당일 snapshot, 광고 잠금."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import errors
from app.core.advisory_lock import advisory_xact_lock
from app.core.time_utils import reward_date_for, safe_zone
from app.models.fortune import DailyFortune, FortuneAdSession, FortuneProfile
from app.schemas.fortune import FortuneProfilePut
from app.services import fortune_catalog, fortune_ephemeris, fortune_rules, gating, privacy
from app.services.account import _load_profile

_MIN_BIRTH_DATE = date(1900, 1, 1)
_RESULT_SCHEMA_VERSION = 3


def _err(code: str, status: int, message: str) -> errors.AppError:
    return errors.AppError(code, status, message)


def _ready() -> bool:
    if not settings.fortune_enabled:
        return False
    rules = fortune_rules.load_rule_assets()
    if not rules["approved_for_production"] and settings.environment not in {"local", "development"}:
        return False
    # feature가 켜질 때 asset 누락·hash 불일치를 즉시 실패시킨다.
    fortune_catalog.load_catalog()
    return True


def _require_enabled() -> None:
    if not _ready():
        raise _err("FEATURE_UNAVAILABLE", 403, "오늘의 운세 기능을 사용할 수 없어요.")


def _locale(_value: str | None, _fallback: str | None = None) -> str:
    # 개발 seed는 한국어만 승인됐다. 응답의 locale도 실제 콘텐츠 언어인 ko를 명시한다.
    return "ko"


def _age_cutoff(today: date) -> date:
    try:
        return today.replace(year=today.year - 14)
    except ValueError:
        return today.replace(year=today.year - 14, day=28)


def _validate_birth_date(birth_date: date, *, today: date) -> None:
    if birth_date < _MIN_BIRTH_DATE:
        raise _err("INVALID_BIRTH_DATE", 422, "지원하지 않는 생년월일이에요.")
    if birth_date > _age_cutoff(today):
        raise _err("UNDER_MINIMUM_AGE", 422, "만 14세 이상만 사용할 수 있어요.")


def _profile_wire(row: FortuneProfile) -> dict[str, Any]:
    return {
        "gender": row.gender,
        "birth_date": row.birth_date,
        "revision": row.revision,
    }


async def get_profile(session: AsyncSession, user_id: str) -> dict[str, Any]:
    _require_enabled()
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    row = await session.get(FortuneProfile, uid)
    if row is None:
        raise _err("PROFILE_REQUIRED", 404, "운세 정보를 먼저 입력해 주세요.")
    return {"profile": _profile_wire(row)}


async def put_profile(
    session: AsyncSession,
    user_id: str,
    req: FortuneProfilePut,
    *,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    _require_enabled()
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    await advisory_xact_lock(session, uid)
    account = await _load_profile(session, user_id)
    now = now_utc or datetime.now(timezone.utc)
    today = reward_date_for(now, account.timezone)
    _validate_birth_date(req.birth_date, today=today)

    current = await session.get(FortuneProfile, uid, with_for_update=True)
    changed = current is None or current.birth_date != req.birth_date or current.gender != req.gender
    if current is None:
        current = FortuneProfile(
            user_id=uid,
            gender=req.gender,
            birth_date=req.birth_date,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(current)
    elif changed:
        current.gender = req.gender
        current.birth_date = req.birth_date
        current.revision += 1
        current.updated_at = now

    daily = await session.get(DailyFortune, uid, with_for_update=True)
    today_result_exists = bool(daily is not None and daily.fortune_date == today)
    unlock_preserved = bool(
        changed
        and daily is not None
        and daily.fortune_date == today
        and daily.unlock_state == "unlocked"
    )
    await session.commit()
    return {
        "profile": _profile_wire(current),
        "result_invalidated": bool(changed and today_result_exists),
        "unlock_preserved": unlock_preserved,
    }


async def delete_profile(session: AsyncSession, user_id: str) -> None:
    _require_enabled()
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    await advisory_xact_lock(session, uid)
    row = await session.get(FortuneProfile, uid, with_for_update=True)
    if row is not None:
        await session.delete(row)
    await session.commit()


async def _access(
    session: AsyncSession,
    user_id: str,
    *,
    now: datetime,
    daily: DailyFortune | None,
    today: date,
    account: Any | None = None,
) -> tuple[str, str]:
    # 당일 공개 권한은 결과 freshness(profile revision/schema)와 독립적이다.
    if daily is not None and daily.fortune_date == today and daily.unlock_state == "unlocked":
        return "unlocked_today", "free"
    plan = await gating.resolve_plan(session, user_id, now, profile=account)
    return ("included" if plan != "free" else "ad_required"), plan


def _versions(row: DailyFortune) -> dict[str, str]:
    return {
        "ephemeris": row.ephemeris_version,
        "rules": row.rule_version,
        "copy": row.copy_version,
    }


def _public_result(row: DailyFortune, locale: str) -> dict[str, Any]:
    actual_locale = locale if locale in row.copy_by_locale else "ko"
    rendered = row.copy_by_locale[actual_locale]
    semantic = row.semantic_result
    categories = {
        category: {
            "score": int(semantic["categories"][category]["score"]),
            "text": list(rendered["categories"][category]["text"]),
        }
        for category in ("love", "money", "work", "energy")
    }
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "locale": actual_locale,
        "overall": {
            "score": int(semantic["overall"]["score"]),
            "headline": rendered["overall"]["headline"],
            "flow": list(rendered["overall"]["flow"]),
            "do": rendered["overall"]["do"],
            "pause": rendered["overall"]["pause"],
        },
        "categories": categories,
        "lucky_color": dict(rendered["lucky_color"]),
    }


def _current_row(
    row: DailyFortune | None,
    *,
    today: date,
    timezone_name: str,
    revision: int,
) -> bool:
    return bool(
        row is not None
        and row.fortune_date == today
        and row.timezone_snapshot == timezone_name
        and row.profile_revision == revision
        and row.result_schema_version == _RESULT_SCHEMA_VERSION
        and row.semantic_result.get("schema_version") == _RESULT_SCHEMA_VERSION
        and "ko" in row.copy_by_locale
    )


async def status(
    session: AsyncSession,
    user_id: str,
    *,
    locale: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    if not _ready():
        return {"available": False}
    now = now_utc or datetime.now(timezone.utc)
    account = await _load_profile(session, user_id)
    today = reward_date_for(now, account.timezone)
    profile = await session.get(FortuneProfile, uid)
    daily = await session.get(DailyFortune, uid) if profile is not None else None
    access, _plan = await _access(
        session, user_id, now=now, daily=daily, today=today, account=account
    )
    if profile is None:
        return {
            "available": True,
            "state": "profile_required",
            "access": access,
        }
    current = _current_row(
        daily,
        today=today,
        timezone_name=account.timezone,
        revision=profile.revision,
    )
    if current and daily is not None and daily.revealed_at is not None:
        lang = _locale(locale, account.language)
        return {
            "available": True,
            "state": "revealed",
            "access": access,
            "local_date": today,
            "result": _public_result(daily, lang),
            "versions": _versions(daily),
        }
    return {
        "available": True,
        "state": "locked" if current else "unseen",
        "access": access,
        "local_date": today,
    }


def _build_result(
    *,
    profile: FortuneProfile,
    today: date,
    timezone_name: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    birth_positions = fortune_ephemeris.date_chart_longitudes(
        profile.birth_date,
        "UTC",
        fortune_ephemeris.BIRTH_PLANET_KEYS,
    )
    current_positions = fortune_ephemeris.date_chart_longitudes(
        today,
        timezone_name,
        fortune_ephemeris.PLANET_KEYS,
    )
    semantic = fortune_rules.generate_semantic_result(
        birth_positions=birth_positions,
        current_positions=current_positions,
        allow_unapproved=settings.environment in {"local", "development"},
    )
    return semantic, fortune_catalog.render_all(semantic)


async def reveal(
    session: AsyncSession,
    user_id: str,
    *,
    locale: str | None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    _require_enabled()
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    await advisory_xact_lock(session, uid)
    now = now_utc or datetime.now(timezone.utc)
    account = await _load_profile(session, user_id)
    today = reward_date_for(now, account.timezone)
    next_midnight = datetime.combine(
        today + timedelta(days=1), time.min, tzinfo=safe_zone(account.timezone)
    ).astimezone(timezone.utc)
    if next_midnight - now < timedelta(minutes=2):
        raise _err("DATE_ROLLOVER", 409, "날짜가 바뀌고 있어요. 잠시 후 다시 시도해 주세요.")
    profile = await session.get(FortuneProfile, uid, with_for_update=True)
    if profile is None:
        raise _err("PROFILE_REQUIRED", 404, "운세 정보를 먼저 입력해 주세요.")
    row = await session.get(DailyFortune, uid, with_for_update=True)
    preserve_unlock = bool(
        row is not None and row.fortune_date == today and row.unlock_state == "unlocked"
    )
    preserved_source = row.unlock_source if preserve_unlock and row is not None else None
    preserved_unlocked_at = row.unlocked_at if preserve_unlock and row is not None else None
    if not _current_row(
        row,
        today=today,
        timezone_name=account.timezone,
        revision=profile.revision,
    ):
        semantic, copies = _build_result(
            profile=profile,
            today=today,
            timezone_name=account.timezone,
        )
        if row is None:
            row = DailyFortune(user_id=uid, created_at=now)
            session.add(row)
        row.fortune_date = today
        row.timezone_snapshot = account.timezone
        row.profile_revision = profile.revision
        row.result_schema_version = _RESULT_SCHEMA_VERSION
        row.semantic_result = semantic
        row.copy_by_locale = copies
        row.ephemeris_version = fortune_ephemeris.EPHEMERIS_VERSION
        row.rule_version = str(fortune_rules.load_rule_assets()["rule_version"])
        row.copy_version = fortune_catalog.COPY_VERSION
        row.updated_at = now
        if preserve_unlock:
            row.unlock_state = "unlocked"
            row.unlock_source = preserved_source
            row.unlocked_at = preserved_unlocked_at
            row.revealed_at = now
        else:
            row.unlock_state = "locked"
            row.unlock_source = None
            row.unlocked_at = None
            row.revealed_at = None

    access, plan = await _access(
        session, user_id, now=now, daily=row, today=today, account=account
    )
    if access == "ad_required":
        verified = (
            await session.execute(
                select(FortuneAdSession.session_id).where(
                    FortuneAdSession.user_id == uid,
                    FortuneAdSession.fortune_date == today,
                    FortuneAdSession.verified.is_(True),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if verified is not None:
            row.unlock_state = "unlocked"
            row.unlock_source = "rewarded_ad"
            row.unlocked_at = row.unlocked_at or now
            row.revealed_at = now
            access = "unlocked_today"
    elif row.unlock_state != "unlocked":
        row.unlock_state = "unlocked"
        row.unlock_source = "trial" if plan == "trial" else "subscription"
        row.unlocked_at = now
        row.revealed_at = now
        access = "included"

    if row.unlock_state == "locked":
        await session.commit()
        return {
            "state": "locked",
            "access": "ad_required",
            "local_date": today,
        }
    if row.revealed_at is None:
        row.revealed_at = now
    row.updated_at = now
    await session.commit()
    lang = _locale(locale, account.language)
    return {
        "state": "revealed",
        "access": access,
        "local_date": today,
        "result": _public_result(row, lang),
        "versions": _versions(row),
    }


async def create_ad_session(
    session: AsyncSession,
    user_id: str,
    *,
    client_request_id: uuid.UUID,
    now_utc: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    _require_enabled()
    uid = uuid.UUID(user_id)
    await privacy.ensure_subject_active(session, uid)
    await advisory_xact_lock(session, uid)
    now = now_utc or datetime.now(timezone.utc)
    account = await _load_profile(session, user_id)
    today = reward_date_for(now, account.timezone)
    profile = await session.get(FortuneProfile, uid)
    if profile is None:
        raise _err("PROFILE_REQUIRED", 404, "운세 정보를 먼저 입력해 주세요.")
    existing = (
        await session.execute(
            select(FortuneAdSession).where(
                FortuneAdSession.user_id == uid,
                FortuneAdSession.client_request_id == client_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _ad_session_wire(existing), False
    daily = await session.get(DailyFortune, uid, with_for_update=True)
    access, _plan = await _access(
        session, user_id, now=now, daily=daily, today=today, account=account
    )
    if (
        access != "ad_required"
        or not _current_row(
            daily,
            today=today,
            timezone_name=account.timezone,
            revision=profile.revision,
        )
        or daily is None
        or daily.unlock_state != "locked"
    ):
        raise _err("AD_NOT_REQUIRED", 403, "운세 광고가 필요하지 않아요.")
    next_midnight = datetime.combine(
        today + timedelta(days=1), time.min, tzinfo=safe_zone(account.timezone)
    ).astimezone(timezone.utc)
    if next_midnight - now < timedelta(minutes=2):
        raise _err("DATE_ROLLOVER", 409, "날짜가 바뀌고 있어요. 잠시 후 다시 시도해 주세요.")
    row = FortuneAdSession(
        user_id=uid,
        fortune_date=today,
        client_request_id=client_request_id,
        expires_at=min(now + timedelta(minutes=30), next_midnight),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return _ad_session_wire(row), True


def _ad_session_wire(row: FortuneAdSession) -> dict[str, Any]:
    return {
        "session_id": row.session_id,
        "custom_data": f"fortune:{row.session_id}",
        "admob_user_id": row.user_id,
        "expires_at": row.expires_at,
    }


def result_fingerprint(row: DailyFortune, locale: str) -> str:
    actual_locale = locale if locale in row.copy_by_locale else "ko"
    payload = {
        "date": row.fortune_date.isoformat(),
        "timezone": row.timezone_snapshot,
        "revision": row.profile_revision,
        "schema": row.result_schema_version,
        "locale": actual_locale,
        "ephemeris": row.ephemeris_version,
        "rules": row.rule_version,
        "copy": row.copy_version,
        "semantic": row.semantic_result,
        "localized": row.copy_by_locale.get(actual_locale),
        "revealed_at": row.revealed_at.isoformat() if row.revealed_at else None,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

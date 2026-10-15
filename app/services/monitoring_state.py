"""워커 알림의 작은 지속 상태: 관측 → lease claim → 전송 성공 CAS ack.

상태 key는 신호별 고정 집합이다. 전송 중 DB lock을 보유하지 않는다.
Slack 성공 뒤 ack 전 종료되는 경우의 중복은 허용하며 exactly-once를 주장하지 않는다.
"""
from __future__ import annotations

import logging
import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.core.db import get_sessionmaker
from app.models.app_config import AppConfig
from app.services import slack_notify

_log = logging.getLogger(__name__)
_SIGNALS = frozenset({"worker_diary", "worker_memory", "worker_timeout", "worker_push", "rc_processor",
                      "rc_observation", "rc_provider", "rc_production", "rc_unknown", "daily_status"})
_LEASE_S = 60
_CHANGE_S = 3600
_REMINDER_S = 21600
_FLAP_S = 300


@dataclass(frozen=True)
class Observation:
    signal: str
    active: bool | None
    fingerprint: str
    message: str
    recovery: str = "미해결 0건 재관측"
    severity: str = "alert"
    daily: bool = False
    importance: int = 1


@dataclass(frozen=True)
class Claim:
    key: str
    token: str
    active: bool
    fingerprint: str
    severity: str
    daily: bool
    message: str
    importance: int = 1
    incident_number: int = 0


def environment() -> str:
    raw = settings.environment.lower()
    return raw if raw in {"production", "development", "local", "test"} else "unknown"


def key_for(signal: str) -> str:
    if signal not in _SIGNALS:
        raise ValueError("unsupported monitoring signal")
    return f"monitoring:alert:{environment()}:{signal}"


def _date(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def transition(state: dict, obs: Observation, now: datetime) -> tuple[dict, bool]:
    """네트워크/DB와 분리한 관측·전송 판정. unknown/오래된 관측은 상태를 지우지 않는다."""
    state = dict(state)
    seen = _date(state.get("observed_at"))
    if obs.active is None or (seen and now < seen):
        return state, False
    was_active = state.get("active", False)
    state.update(observed_at=now.isoformat(), active=obs.active,
                 fingerprint=obs.fingerprint, severity=obs.severity, importance=obs.importance)
    if obs.active and not was_active:
        state["opened_at"] = now.isoformat()
        state["had_incident"] = True
        state["incident_number"] = state.get("incident_number", 0) + 1
    lease_until = _date(state.get("lease_until"))
    retry_after = _date(state.get("retry_after"))
    if (lease_until and now < lease_until) or (retry_after and now < retry_after):
        return state, False
    last = _date(state.get("last_notified_at"))
    if obs.daily:
        return state, state.get("last_notified_date") != obs.fingerprint
    if not obs.active:
        # 초기 정상은 무통지. 전송에 실패한 장애도 정상 재관측 시 조용히 소실시키지 않는다.
        unsent_incident = state.get("incident_number", 0) > state.get("notified_incident_number", 0)
        return state, bool(state.get("had_incident")) and (
            state.get("notified_active") is not False or unsent_incident
        )
    if not last:
        return state, True
    elapsed = (now - last).total_seconds()
    if state.get("notified_active") is False:
        return state, elapsed >= _FLAP_S
    if (obs.importance > state.get("notified_importance", 1)
            or (obs.severity == "alert" and state.get("notified_severity") != "alert")):
        return state, elapsed >= _FLAP_S
    if state.get("notified_fingerprint") != obs.fingerprint:
        return state, elapsed >= _CHANGE_S
    return state, elapsed >= _REMINDER_S


async def _claim(session, obs: Observation, now: datetime) -> Claim | None:
    """호출자가 트랜잭션을 커밋한다. 테스트는 동일 계약을 rollback으로 검증한다."""
    key = key_for(obs.signal)
    await session.execute(insert(AppConfig).values(key=key, value={}).on_conflict_do_nothing())
    row = (await session.execute(select(AppConfig).where(AppConfig.key == key)
                                 .with_for_update())).scalar_one()
    state, due = transition(row.value or {}, obs, now)
    claim = None
    if due:
        token = str(uuid.uuid4())
        state.update(lease_token=token, lease_until=(now + timedelta(seconds=_LEASE_S)).isoformat())
        message = obs.message if obs.active else obs.recovery
        if not obs.active and state.get("notified_active") is not True:
            message = "지연 통지 — 일시 실패 후 " + message
        claim = Claim(key, token, bool(obs.active), obs.fingerprint, obs.severity,
                      obs.daily, message, obs.importance, state.get("incident_number", 0))
    row.value = state
    row.updated_at = now
    return claim


async def _ack(session, claim: Claim, *, sent: bool, now: datetime) -> bool:
    row = (await session.execute(select(AppConfig).where(AppConfig.key == claim.key)
                                 .with_for_update())).scalar_one_or_none()
    if row is None or (row.value or {}).get("lease_token") != claim.token:
        return False
    state = dict(row.value)
    state.pop("lease_token", None)
    state.pop("lease_until", None)
    if sent:
        state.update(last_notified_at=now.isoformat(), notified_active=claim.active,
                     notified_fingerprint=claim.fingerprint, notified_severity=claim.severity,
                     notified_importance=claim.importance,
                     notified_incident_number=claim.incident_number, failures=0)
        state.pop("retry_after", None)
        if claim.daily:
            state["last_notified_date"] = claim.fingerprint
    else:
        failures = min(int(state.get("failures", 0)) + 1, 5)
        delay = min(900, (60 if claim.severity == "alert" else 300) * 2 ** (failures - 1))
        state.update(failures=failures, retry_after=(now + timedelta(seconds=delay)).isoformat())
    # token 검증을 SQL에도 둬 늦은 sender가 새 claim을 ack할 수 없게 한다.
    result = await session.execute(update(AppConfig).where(
        AppConfig.key == claim.key, AppConfig.value["lease_token"].astext == claim.token,
    ).values(value=state, updated_at=now))
    return result.rowcount == 1


async def publish(
    obs: Observation, now: datetime, *, render: Callable[[], Awaitable[str]] | None = None,
) -> bool:
    if obs.signal not in _SIGNALS:
        raise ValueError("unsupported monitoring signal")
    if obs.active is None:
        return False
    try:
        async with asyncio.timeout(3):
            async with get_sessionmaker()() as session:
                claim = await _claim(session, obs, now)
                await session.commit()
    except Exception as exc:  # noqa: BLE001
        _log.warning("알림 상태 관측 실패 signal=%s error=%s", obs.signal, type(exc).__name__)
        if obs.active and obs.severity == "alert":
            return await slack_notify.send(
                f"[{environment()}] {obs.message}\n알림 상태 저장 불가",
                severity="alert", dedup_key=f"state_unavailable:{obs.signal}",
            )
        return False
    if claim is None:
        return False
    sent = False
    try:
        message = await render() if render else claim.message
        sent = await slack_notify.send(f"[{environment()}] {message}", severity=claim.severity)
    except Exception as exc:  # noqa: BLE001
        _log.warning("알림 준비/전송 실패 signal=%s error=%s", obs.signal, type(exc).__name__)
    try:
        async with asyncio.timeout(3):
            async with get_sessionmaker()() as session:
                await _ack(session, claim, sent=sent, now=datetime.now(timezone.utc))
                await session.commit()
    except Exception as exc:  # noqa: BLE001  lease 만료 뒤 재시도 가능.
        _log.warning("알림 전송 확인 기록 실패 signal=%s error=%s", obs.signal, type(exc).__name__)
    return sent

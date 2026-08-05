"""도구 루프 설정 해석(W5-1) — `app_config` override 우선, `Settings`는 fallback.

토큰 한도(`app/services/limits.py`의 `_KEYS`)와 **별개 계약**이다. 두 표를 섞으면 한쪽 롤아웃이
다른 쪽 값을 흔든다 — 키도 함수도 공유하지 않는다.

조회는 Phase 1 read-only 구간에서 **1회**뿐이고 결과는 frozen `AgentConfigSnapshot`으로 복사한다.
agent phase는 DB도 `settings`도 다시 읽지 않는다. 프로세스 전역 TTL 캐시는 두지 않는다 —
요청당 DB 1회라 override가 다음 턴부터 곧바로 반영되고 두 EC2의 캐시가 어긋나지 않는다.

우선순위/fallback: 키별로 `app_config.value`가 있고 검증을 통과하면 그 값을, 누락·JSON 타입 불일치·
범위 위반이면 **그 키만** `settings.<key>`로 내린다. DB 조회 자체의 실패는 여기서 삼키지 않고
호출측(Phase 1)의 DB 오류로 전파된다 — 설정 장애를 숨기려고 임의 fallback하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.services import slack_notify
from app.services.config_store import get_config_values

_log = logging.getLogger("moly-backend")

AGENT_CONFIG_KEYS = (
    "agent_enabled",
    "agent_turn_deadline_s",
    "agent_final_reserve_s",
    "agent_max_tool_rounds",
    "agent_max_tool_calls_per_turn",
    "agent_decide_max_tokens",
    "agent_tool_result_budget_tokens",
    "agent_tool_timeout_ms",
    "agent_tool_inflight",
    "agent_canary_pct",
)

Source = Literal["app_config", "settings"]

# --- 비용 불변식(명세 §3.1.3) ---
# D=agent_decide_max_tokens(첫 홉 출력 상한), T=agent_tool_result_budget_tokens(도구 결과 턴 합계).
# 개별 상한(D≤214·T≤732)은 각각 **상대가 기본값일 때의** 최대치라 둘 다 개별 범위를 통과해도
# 조합이 턴 수 제약(-20%)을 깰 수 있다. 그래서 조합 부등식을 따로 확인한다.
#   예) D=214,T=700 → 2,426.5 > 2,307 → 거부. 기본값 192/600 → 2,142(여유 165).
_COST_D_COEF = 7.25
_COST_T_COEF = 1.25
_COST_LIMIT = 2307
# 부등식 위반 시 되돌아갈 **코드** 기본값. `settings`가 아니라 상수다 — env가 위반값이어도 여기로 내린다.
DEFAULT_DECIDE_MAX_TOKENS = 192
DEFAULT_TOOL_RESULT_BUDGET_TOKENS = 600

# 각 키의 코드 기본값 — `settings` 값마저 범위를 벗어날 때의 최후 fallback(부팅 env 오설정 방어).
_CODE_DEFAULTS: dict[str, Any] = {
    "agent_enabled": False,
    "agent_turn_deadline_s": 8.0,  # 실측 근거는 settings.agent_turn_deadline_s 주석 참조
    "agent_final_reserve_s": 2.5,
    "agent_max_tool_rounds": 1,
    "agent_max_tool_calls_per_turn": 3,
    "agent_decide_max_tokens": DEFAULT_DECIDE_MAX_TOKENS,
    "agent_tool_result_budget_tokens": DEFAULT_TOOL_RESULT_BUDGET_TOKENS,
    "agent_tool_timeout_ms": 800,
    "agent_tool_inflight": 8,
    "agent_canary_pct": 0.0,
}

# production 판정 — 이 목록에 없는 environment는 전부 production으로 본다(default-deny).
# 명세는 "production에서 fail-closed"지만 배포 환경변수 표기가 prod/production 어느 쪽인지
# 코드에 고정돼 있지 않다. 오타·미설정으로 경보가 **빠지는** 쪽보다 도는 쪽이 안전하다.
# 개발·shadow(local/dev)는 `settings` 초기값(2.5·8)만으로 켤 수 있어야 해서 면제한다.
_NON_PRODUCTION_ENVS = frozenset({"local", "dev", "development", "test", "ci"})

# `enabled=True`를 production에서 인정하려면 반드시 app_config에 명시돼야 하는 "측정 필요" 키.
_MEASURED_KEYS = ("agent_final_reserve_s", "agent_tool_inflight")

_alert_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class AgentConfigSnapshot:
    """그 턴 동안 고정되는 도구 루프 설정. 조립 후에는 값도 출처도 바뀌지 않는다."""

    enabled: bool
    turn_deadline_s: float
    final_reserve_s: float
    max_tool_rounds: int
    max_tool_calls_per_turn: int
    decide_max_tokens: int
    tool_result_budget_tokens: int
    tool_timeout_ms: int
    tool_inflight: int
    canary_pct: float
    source: Mapping[str, Source]

    def from_app_config(self, key: str) -> bool:
        """그 키가 DB override에서 왔는지. production fail-closed 판정과 관측에 쓴다."""
        return self.source.get(key) == "app_config"


def _alert(message: str, *, dedup_key: str) -> None:
    """경보 = ERROR 로그(항상) + 슬랙 best-effort.

    슬랙 전송을 await하지 않는다 — 이 함수는 유저 요청 경로(Phase 1)에서 불리고 웹훅은 최대 10초라
    응답 시간 제약(§0.1)을 그대로 깨뜨린다. 웹훅 미설정(로컬·테스트)이면 태스크도 만들지 않는다.
    """
    _log.error("%s", message)
    if not (settings.slack_alert_webhook_url or settings.slack_webhook_url):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 동기 컨텍스트(스크립트) 호출
        return
    task = loop.create_task(slack_notify.alert(message, dedup_key=dedup_key))
    _alert_tasks.add(task)  # 강참조 유지(GC가 실행 중 태스크를 수거하지 않게)
    task.add_done_callback(_alert_tasks.discard)


# --- 타입 캐스트: JSON 타입이 정확히 맞을 때만 값을 통과시킨다 ---
def _as_bool(v: Any) -> bool | None:
    """JSON bool만. 1/0/"true"는 bool이 아니다(킬스위치를 숫자로 켜지 못하게)."""
    return v if isinstance(v, bool) else None


def _as_int(v: Any) -> int | None:
    """JSON 정수만. bool은 int의 서브클래스라 명시적으로 배제한다."""
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _as_float(v: Any) -> float | None:
    """유한 숫자(int/float)만. bool 배제, NaN·Inf 배제."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


class _Resolver:
    """키별 해석기 — app_config → settings → 코드 기본값 순으로 내려가며 출처를 기록한다."""

    def __init__(self, raw: Mapping[str, Any]):
        self._raw = raw
        self.source: dict[str, Source] = {}

    def pick(
        self,
        key: str,
        cast: Callable[[Any], Any],
        check: Callable[[Any], bool],
    ) -> Any:
        if key in self._raw:
            value = cast(self._raw[key])
            if value is None:
                self._warn(key, "type")
            elif not check(value):
                self._warn(key, "range")
            else:
                self.source[key] = "app_config"
                return value
        # 누락은 정상 상태다(override 미설정). 턴마다 10줄씩 경고를 찍지 않고 debug로만 남긴다 —
        # 값이 있는데 불량인 경우만 warning이다(그건 운영자가 방금 잘못 넣은 것이다).
        self.source[key] = "settings"
        fallback = getattr(settings, key)
        if cast(fallback) is None or not check(cast(fallback)):
            # env 값마저 계약을 어기면 코드 기본값으로 — 설정 오배포가 런타임 계약을 깨지 못하게.
            _log.warning("agent config settings 값도 무효 key=%s reason=settings_invalid", key)
            return _CODE_DEFAULTS[key]
        return cast(fallback)

    def _warn(self, key: str, reason: str) -> None:
        # 값 자체는 남기지 않는다(운영 설정이지만 로그 표면 최소화). key/reason만 있으면 특정된다.
        _log.warning("agent config override 무시 key=%s reason=%s", key, reason)


def build_snapshot(raw: Mapping[str, Any]) -> AgentConfigSnapshot:
    """조회 결과(dict) → 검증된 frozen snapshot. DB 접근이 없어 순수 함수로 테스트한다."""
    r = _Resolver(raw)

    enabled = r.pick("agent_enabled", _as_bool, lambda v: True)
    # 상한 12초. 예전엔 5.0이었는데, 그러면 실측으로 5초가 부족하다고 판명돼도 **app_config로
    # 못 늘린다** — override가 조용히 거부되고 5.0으로 되돌아간다. 실제로 그 상태에서 1홉
    # timeout이 25%였다. 상한은 남기되(무한정 대기 방지) 튜닝 여지 위에 둔다.
    turn_deadline_s = r.pick("agent_turn_deadline_s", _as_float, lambda v: 0 < v <= 12.0)
    # final reserve는 **해석된** turn deadline 기준으로 검증한다(둘 다 override일 수 있어 순서가 있다).
    final_reserve_s = r.pick("agent_final_reserve_s", _as_float, lambda v: 0 < v < turn_deadline_s)
    max_tool_rounds = r.pick("agent_max_tool_rounds", _as_int, lambda v: v == 1)
    max_tool_calls = r.pick("agent_max_tool_calls_per_turn", _as_int, lambda v: 1 <= v <= 3)
    decide_max_tokens = r.pick("agent_decide_max_tokens", _as_int, lambda v: 1 <= v <= 214)
    tool_result_budget = r.pick("agent_tool_result_budget_tokens", _as_int, lambda v: 1 <= v <= 732)
    tool_timeout_ms = r.pick("agent_tool_timeout_ms", _as_int, lambda v: 1 <= v <= 800)
    tool_inflight = r.pick("agent_tool_inflight", _as_int, lambda v: 1 <= v <= 64)
    canary_pct = r.pick("agent_canary_pct", _as_float, lambda v: 0.0 <= v <= 100.0)

    # 두 값이 서로 다른 출처에서 와 `reserve >= deadline`이 될 수 있다(예: deadline만 override로 1.0,
    # reserve는 settings 2.5). 그 조합이면 도구를 시작할 수 있는 창이 음수라 루프가 성립하지 않는다.
    if final_reserve_s >= turn_deadline_s:
        final_reserve_s = turn_deadline_s / 2
        r.source["agent_final_reserve_s"] = "settings"
        _log.warning("agent config final_reserve 재조정 key=agent_final_reserve_s reason=ge_deadline")

    # 비용 불변식 — 개별 범위를 둘 다 통과해도 조합이 위반이면 **쌍으로** 코드 기본값으로 되돌린다.
    cost = _COST_D_COEF * decide_max_tokens + _COST_T_COEF * tool_result_budget
    if cost > _COST_LIMIT:
        _alert(
            "agent 비용 불변식 위반 — decide_max_tokens=%d, tool_result_budget_tokens=%d "
            "(7.25D+1.25T=%.1f > %d). override 거부하고 코드 기본값(%d/%d)으로 되돌린다."
            % (
                decide_max_tokens,
                tool_result_budget,
                cost,
                _COST_LIMIT,
                DEFAULT_DECIDE_MAX_TOKENS,
                DEFAULT_TOOL_RESULT_BUDGET_TOKENS,
            ),
            dedup_key="agent_cost_invariant",
        )
        decide_max_tokens = DEFAULT_DECIDE_MAX_TOKENS
        tool_result_budget = DEFAULT_TOOL_RESULT_BUDGET_TOKENS
        # 출처는 더 이상 app_config가 아니다(코드 기본값 = settings 계열).
        r.source["agent_decide_max_tokens"] = "settings"
        r.source["agent_tool_result_budget_tokens"] = "settings"

    # production fail-closed — "측정 필요" 값이 app_config로 명시되지 않았으면 켜지 않는다.
    if enabled and _is_production() and not all(r.source.get(k) == "app_config" for k in _MEASURED_KEYS):
        missing = [k for k in _MEASURED_KEYS if r.source.get(k) != "app_config"]
        _alert(
            "agent 활성화 거부(fail-closed) — production에서는 %s 가 app_config에 명시돼야 한다"
            % ", ".join(missing),
            dedup_key="agent_enable_fail_closed",
        )
        enabled = False

    return AgentConfigSnapshot(
        enabled=enabled,
        turn_deadline_s=turn_deadline_s,
        final_reserve_s=final_reserve_s,
        max_tool_rounds=max_tool_rounds,
        max_tool_calls_per_turn=max_tool_calls,
        decide_max_tokens=decide_max_tokens,
        tool_result_budget_tokens=tool_result_budget,
        tool_timeout_ms=tool_timeout_ms,
        tool_inflight=tool_inflight,
        canary_pct=canary_pct,
        source=MappingProxyType(dict(r.source)),
    )


def _is_production() -> bool:
    return settings.environment.strip().lower() not in _NON_PRODUCTION_ENVS


async def effective_agent_config(session: AsyncSession) -> AgentConfigSnapshot:
    """Phase 1 read-only 구간에서 1회 호출. 조회 실패는 잡지 않는다(Phase 1 DB 오류로 전파)."""
    raw = await get_config_values(session, list(AGENT_CONFIG_KEYS))
    return build_snapshot(raw)

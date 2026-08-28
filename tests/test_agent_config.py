"""W5-1 — agent 설정 해석(app_config override + settings fallback).

핵심 회귀: 비용 부등식(7.25D + 1.25T ≤ 2307)이 개별 범위와 **별도로** 강제되는지,
키별 fallback이 그 키에만 국한되는지, production에서 측정값 없이 켜지지 않는지.
"""
import logging

import pytest

from app.services.agent import config as agent_config
from app.services.agent.config import (
    AGENT_CONFIG_KEYS,
    DEFAULT_DECIDE_MAX_TOKENS,
    DEFAULT_TOOL_RESULT_BUDGET_TOKENS,
    build_snapshot,
)

# production fail-closed를 통과시키는 최소 override(측정 필요 키 2개).
_MEASURED = {"agent_final_reserve_s": 2.5, "agent_tool_inflight": 8}


def test_keys_are_separate_from_token_limit_contract():
    """토큰 한도(limits.CONFIG_KEYS)와 키를 섞지 않는다 — 한쪽 롤아웃이 다른 쪽을 흔들면 안 된다."""
    from app.services.limits import CONFIG_KEYS as TOKEN_KEYS

    assert set(AGENT_CONFIG_KEYS).isdisjoint(TOKEN_KEYS)
    assert all(k.startswith("agent_") for k in AGENT_CONFIG_KEYS)


def test_empty_app_config_falls_back_to_settings_defaults():
    snap = build_snapshot({})
    assert snap.enabled is False  # 킬스위치 기본 off
    assert (snap.turn_deadline_s, snap.final_reserve_s) == (8.0, 2.5)
    assert (snap.max_tool_rounds, snap.max_tool_calls_per_turn) == (1, 3)
    assert (snap.decide_max_tokens, snap.tool_result_budget_tokens) == (192, 600)
    assert (snap.tool_timeout_ms, snap.tool_inflight, snap.canary_pct) == (800, 8, 0.0)
    assert set(snap.source.values()) == {"settings"}


def test_snapshot_is_frozen_and_source_immutable():
    snap = build_snapshot({})
    with pytest.raises(Exception):
        snap.enabled = True  # type: ignore[misc]
    with pytest.raises(TypeError):
        snap.source["agent_enabled"] = "app_config"  # type: ignore[index]


# --- 비용 불변식: 개별 범위만으로는 부족하다 ---
def test_cost_invariant_rejects_valid_individually_but_invalid_combination(caplog):
    """D=214·T=700 — 개별 범위는 둘 다 통과하지만 2,426.5 > 2,307이라 거부돼야 한다."""
    caplog.set_level(logging.ERROR, logger="moly-backend")
    snap = build_snapshot({"agent_decide_max_tokens": 214, "agent_tool_result_budget_tokens": 700})

    assert snap.decide_max_tokens == DEFAULT_DECIDE_MAX_TOKENS == 192
    assert snap.tool_result_budget_tokens == DEFAULT_TOOL_RESULT_BUDGET_TOKENS == 600
    assert snap.source["agent_decide_max_tokens"] == "settings"
    assert snap.source["agent_tool_result_budget_tokens"] == "settings"
    assert any("비용 불변식" in r.message for r in caplog.records)  # 경보


@pytest.mark.parametrize(
    ("d", "t", "accepted"),
    [
        (214, 600, True),   # 한쪽만 올린 최대치(2,301.5)
        (192, 732, True),   # 반대쪽 최대치(2,307.0 = 경계 허용)
        (193, 601, True),   # 소폭 동시 상향(2,150.5)
        (214, 732, False),  # 둘 다 최대치 = 2,466.5 위반
        (214, 700, False),  # 명세가 못박은 반례
        (200, 700, False),  # 2,325 — 개별은 통과, 조합은 위반
    ],
)
def test_cost_invariant_boundaries(d, t, accepted):
    snap = build_snapshot(
        {"agent_decide_max_tokens": d, "agent_tool_result_budget_tokens": t}
    )
    if accepted:
        assert (snap.decide_max_tokens, snap.tool_result_budget_tokens) == (d, t)
    else:
        assert (snap.decide_max_tokens, snap.tool_result_budget_tokens) == (192, 600)


def test_cost_invariant_uses_effective_pair_not_only_overrides():
    """한쪽만 override여도 **조합**으로 판정한다(상대는 settings 값)."""
    snap = build_snapshot({"agent_tool_result_budget_tokens": 732})  # D=192(settings) → 2,307 OK
    assert snap.tool_result_budget_tokens == 732
    snap2 = build_snapshot({"agent_decide_max_tokens": 214, "agent_tool_result_budget_tokens": 732})
    assert (snap2.decide_max_tokens, snap2.tool_result_budget_tokens) == (192, 600)


# --- 개별 키 검증·fallback ---
@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("agent_enabled", 1),                    # bool 자리에 숫자 — 킬스위치를 1로 켜지 못한다
        ("agent_enabled", "true"),
        ("agent_turn_deadline_s", 12.5),         # (0, 12.0]
        ("agent_turn_deadline_s", 0),
        ("agent_final_reserve_s", 9.0),          # (0, turn_deadline)
        ("agent_max_tool_rounds", 2),            # ==1
        ("agent_max_tool_calls_per_turn", 4),    # 1..3
        ("agent_decide_max_tokens", 215),        # 1..214
        ("agent_decide_max_tokens", True),       # bool은 int가 아니다
        ("agent_tool_result_budget_tokens", 733),
        ("agent_tool_timeout_ms", 801),          # 1..800
        ("agent_tool_inflight", 65),             # 1..64
        ("agent_tool_inflight", 8.5),            # 정수만
        ("agent_canary_pct", 100.1),             # 0..100
        ("agent_canary_pct", float("nan")),      # 유한값만
    ],
)
def test_invalid_override_falls_back_to_settings_for_that_key_only(key, bad, caplog):
    caplog.set_level(logging.WARNING, logger="moly-backend")
    raw = {"agent_tool_timeout_ms": 500, key: bad}
    snap = build_snapshot(raw)

    assert snap.source[key] == "settings"
    if key != "agent_tool_timeout_ms":  # 다른 정상 키는 그대로 app_config를 쓴다(오염 없음)
        assert snap.tool_timeout_ms == 500
        assert snap.source["agent_tool_timeout_ms"] == "app_config"
    assert any(f"key={key}" in r.getMessage() for r in caplog.records)


def test_missing_key_is_not_warned_but_marked_settings(caplog):
    """누락은 정상 상태(override 미설정)다 — 턴마다 경고를 쏟지 않는다."""
    caplog.set_level(logging.WARNING, logger="moly-backend")
    snap = build_snapshot({})
    assert snap.source["agent_canary_pct"] == "settings"
    assert [r for r in caplog.records if "agent config override 무시" in r.message] == []


def test_valid_overrides_are_used():
    snap = build_snapshot(
        {
            "agent_enabled": True,
            "agent_turn_deadline_s": 4.0,
            "agent_final_reserve_s": 1.5,
            "agent_max_tool_rounds": 1,
            "agent_max_tool_calls_per_turn": 2,
            "agent_decide_max_tokens": 100,
            "agent_tool_result_budget_tokens": 500,
            "agent_tool_timeout_ms": 300,
            "agent_tool_inflight": 4,
            "agent_canary_pct": 12.5,
        }
    )
    assert snap.enabled is True
    assert (snap.turn_deadline_s, snap.final_reserve_s) == (4.0, 1.5)
    assert (snap.tool_timeout_ms, snap.tool_inflight, snap.canary_pct) == (300, 4, 12.5)
    assert set(snap.source.values()) == {"app_config"}


def test_final_reserve_validated_against_effective_turn_deadline():
    """deadline을 줄이면 reserve 범위도 같이 좁아진다(둘 다 override인 경우 순서 의존)."""
    snap = build_snapshot({"agent_turn_deadline_s": 2.0, "agent_final_reserve_s": 3.0})
    assert snap.source["agent_final_reserve_s"] == "settings"
    assert snap.final_reserve_s < snap.turn_deadline_s  # settings(2.5)도 못 쓰므로 재조정


# --- production fail-closed ---
def test_production_requires_measured_keys_from_app_config(monkeypatch, caplog):
    monkeypatch.setattr(agent_config.settings, "environment", "production")
    caplog.set_level(logging.ERROR, logger="moly-backend")
    snap = build_snapshot({"agent_enabled": True})

    assert snap.enabled is False  # fail-closed
    assert any("fail-closed" in r.message for r in caplog.records)


def test_production_enabled_when_measured_keys_present(monkeypatch):
    monkeypatch.setattr(agent_config.settings, "environment", "production")
    snap = build_snapshot({"agent_enabled": True, **_MEASURED})
    assert snap.enabled is True


def test_production_fail_closed_when_only_one_measured_key(monkeypatch):
    monkeypatch.setattr(agent_config.settings, "environment", "production")
    snap = build_snapshot({"agent_enabled": True, "agent_final_reserve_s": 2.0})
    assert snap.enabled is False


@pytest.mark.parametrize("env", ["local", "dev", "test"])
def test_non_production_can_enable_with_settings_defaults(monkeypatch, env):
    monkeypatch.setattr(agent_config.settings, "environment", env)
    assert build_snapshot({"agent_enabled": True}).enabled is True


def test_unknown_environment_is_treated_as_production(monkeypatch):
    """환경 표기가 prod/production 어느 쪽이든(오타 포함) 경보가 빠지지 않는 쪽으로 판정한다."""
    monkeypatch.setattr(agent_config.settings, "environment", "prod")
    assert build_snapshot({"agent_enabled": True}).enabled is False


def test_disabled_snapshot_is_not_fail_closed_alerted(monkeypatch, caplog):
    monkeypatch.setattr(agent_config.settings, "environment", "production")
    caplog.set_level(logging.ERROR, logger="moly-backend")
    assert build_snapshot({}).enabled is False
    assert [r for r in caplog.records if "fail-closed" in r.message] == []


# --- DB 경로 ---
async def test_effective_agent_config_queries_once_and_propagates_db_error():
    calls = []

    class _Session:
        async def execute(self, stmt):
            calls.append(stmt)
            raise RuntimeError("DB 장애")

    with pytest.raises(RuntimeError):  # 설정 장애를 숨기지 않는다(Phase 1 DB 오류로 전파)
        await agent_config.effective_agent_config(_Session())
    assert len(calls) == 1

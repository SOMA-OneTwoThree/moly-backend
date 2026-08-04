"""W5 — 도구 루프(AgentRuntime) + chat 배선.

핵심 회귀: 도구 미호출 턴이 기존 단발 경로와 같은 모양인지 / 데드라인이 모자라면 도구를 아예
시작하지 않는지 / 어떤 실패에도 **모든 call_id의 형식이 닫히는지**(안 닫히면 다음 홉을 못 만든다) /
LLM timeout이 남은 데드라인에 묶이는지 / `agent_enabled=False`에서 회귀가 0인지.
"""
import asyncio
import logging
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.services import chat as chat_service
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services.agent import config as agent_config
from app.services.agent import runtime as agent_runtime
from app.services.agent.config import build_snapshot
from app.services.llm import (
    ControlIntent,
    LlmCall,
    LLMResult,
    StepResult,
    ToolCall,
    ToolResult,
)
from tests.test_chat import FakeSession, _gating

UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SYSTEM = ["페르소나", "기억"]
CONVO = [{"role": "user", "content": "오늘 어땠어?"}]


def _cfg(**over):
    """app_config override 없이 조립한 뒤 필요한 필드만 바꾼 snapshot."""
    from dataclasses import replace

    base = build_snapshot({"agent_enabled": True, "agent_canary_pct": 100.0})
    return replace(base, **over) if over else base


class _Clock:
    """단조 시계 대역 — 가짜 step이 이걸 진행시켜 데드라인 소진을 재현한다."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _usage(purpose: str) -> LlmCall:
    return LlmCall(
        provider="openai", model="gpt-5.6-luna", purpose=purpose,
        input_tokens=10, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0, billable=40,
    )


class _FakeSteps:
    """generate_step 대역 — 호출 kwargs를 그대로 모으고, 넘겨받은 transcript의 형식 완결성을 검증한다."""

    def __init__(self, *steps: StepResult, clock: _Clock | None = None, elapse: float = 0.0):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.clock = clock
        self.elapse = elapse

    async def __call__(self, system, transcript, **kw):
        # 미완결 transcript면 여기서 터진다 — 실제 wire 변환과 같은 함수로 검사한다.
        llm_module.to_openai_messages(system, transcript)
        self.calls.append({"transcript": list(transcript), **kw})
        if self.clock is not None:
            self.clock.advance(self.elapse)
        return self.steps.pop(0) if self.steps else _text_step("응.", purpose="tool_final")


def _text_step(text: str, *, purpose: str = "tool_decide") -> StepResult:
    return StepResult(text=text, tool_calls=[], finish_reason="stop", usage=_usage(purpose))


def _calls_step(*calls: ToolCall) -> StepResult:
    return StepResult(
        text=None, tool_calls=list(calls), finish_reason="tool_calls", usage=_usage("tool_decide")
    )


def _call(i: int, name: str = "search_memory", **kw) -> ToolCall:
    return ToolCall(call_id=f"call_{i}", tool_name=name, arguments={"query": "x"}, **kw)


# --- 가짜 도구·registry·세션 ---
class _FakeTool:
    def __init__(self, name="search_memory", *, boom=None, delay=0.0, data=None, timeout_ms=None):
        self.name = name
        self.timeout_ms = timeout_ms
        self.boom = boom
        self.delay = delay
        self.data = data if data is not None else {"items": ["기억"]}
        self.ran = 0
        self.sessions: list[object] = []

    async def execute(self, ctx, args, session):
        self.ran += 1
        self.sessions.append(session)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.boom is not None:
            raise self.boom
        return ToolResult(
            call_id="ignored", tool_name="ignored", status="ok", data=self.data
        )


class _FakeRegistry:
    def __init__(self, *tools: _FakeTool, control: dict | None = None):
        self._tools = {t.name: t for t in tools}
        self._control = control or {}

    def control_tools(self):
        return self._control

    def wire_schemas(self):
        return [
            {"type": "function", "function": {"name": n, "parameters": {}}} for n in self._tools
        ]

    def input_models(self):
        return {}

    def get(self, name):
        return self._tools.get(name)


class _FakeToolSession:
    """도구 전용 단명 세션 — Phase 1 세션과 다른 객체임을 확인하려고 개수를 센다."""

    opened = 0
    closed = 0
    rolled_back = 0

    async def __aenter__(self):
        type(self).opened += 1
        return self

    async def __aexit__(self, *exc):
        type(self).closed += 1
        return False

    async def rollback(self):
        type(self).rolled_back += 1


def _factory():
    return _FakeToolSession()


@pytest.fixture(autouse=True)
def _reset_tool_session():
    _FakeToolSession.opened = _FakeToolSession.closed = _FakeToolSession.rolled_back = 0
    # W6부터는 registry override가 None이면 **실제** registry(도구 2종)가 붙는다. 이 파일의 관심사는
    # 루프 자체라 도구 구성에 흔들리면 안 되므로, 빈 registry를 꽂아 "도구 없음"을 명시적으로 만든다
    # (registry를 인자로 넘기는 테스트는 그 값이 우선한다).
    agent_runtime.set_registry(_FakeRegistry())
    agent_runtime.SAFETY_CLASSIFIER = None
    yield
    agent_runtime.set_registry(None)
    agent_runtime.SAFETY_CLASSIFIER = None


async def _run(fake, *, registry=None, cfg=None, clock=None, user_text="오늘 어땠어?", monkeypatch):
    monkeypatch.setattr(llm_module, "generate_step", fake)
    return await agent_runtime.run_turn(
        SYSTEM, CONVO,
        config=cfg or _cfg(),
        user_id=UID,
        language="ko",
        activity_date=date(2026, 8, 4),
        user_text=user_text,
        registry=registry,
        session_factory=_factory,
        now=clock or _Clock(),
    )


# --- 1) 도구 미호출 턴 = 기존과 같은 모양(호출 1회) ---
async def test_no_tools_registered_returns_step1_text_with_single_call(monkeypatch):
    fake = _FakeSteps(_text_step("응, 그냥 그랬어.", purpose="chat"))
    turn = await _run(fake, registry=None, monkeypatch=monkeypatch)

    assert turn.text == "응, 그냥 그랬어."
    assert len(fake.calls) == 1  # step 2 없음
    assert fake.calls[0]["tools"] is None  # 도구를 제안조차 하지 않는다
    assert fake.calls[0]["purpose"] == "chat"  # 회계 purpose도 단발 대화와 같다
    assert turn.skipped == "no_tools"
    assert [c.purpose for c in turn.calls] == ["chat"]


async def test_model_answers_without_tool_calls_uses_single_call(monkeypatch):
    fake = _FakeSteps(_text_step("응."))
    turn = await _run(fake, registry=_FakeRegistry(_FakeTool()), monkeypatch=monkeypatch)

    assert turn.text == "응."
    assert len(fake.calls) == 1
    assert fake.calls[0]["tools"] and fake.calls[0]["tool_choice"] == "auto"
    assert turn.skipped == "no_tool_calls"


async def test_step1_max_tokens_is_decide_budget(monkeypatch):
    fake = _FakeSteps(_text_step("응."))
    await _run(fake, registry=_FakeRegistry(_FakeTool()), cfg=_cfg(), monkeypatch=monkeypatch)
    assert fake.calls[0]["max_tokens"] == 192


# --- 2) 데드라인 ---
async def test_llm_timeout_bounded_by_deadline_not_llm_timeout_s(monkeypatch):
    """llm_timeout_s=60을 그대로 쓰면 5초 제약이 그 자리에서 깨진다.

    도구가 있는 턴은 뒤에 최종 호출이 한 번 더 오므로 첫 홉을 예약선(5.0-2.5) 앞에서 끊는다.
    """
    assert chat_service.settings.llm_timeout_s == 60.0
    fake = _FakeSteps(_text_step("응."))
    await _run(fake, registry=_FakeRegistry(_FakeTool()), monkeypatch=monkeypatch)
    assert fake.calls[0]["timeout"] == 2.5


async def test_single_hop_turn_keeps_full_deadline(monkeypatch):
    """도구가 없는 턴은 첫 홉이 곧 최종 답변이다 — 예약분을 떼면 예산만 깎인다."""
    fake = _FakeSteps(_text_step("응."))
    await _run(fake, registry=None, monkeypatch=monkeypatch)
    assert fake.calls[0]["timeout"] == 5.0


async def test_worst_case_turn_stays_within_deadline(monkeypatch):
    """첫 홉이 제 상한을 꽉 채워도 턴 합계가 turn_deadline_s를 넘지 않는다.

    첫 홉이 예약분을 남기지 않으면(reserve=0) 최종 호출에 floor(=2.5초)가 그대로 더 붙어
    합계가 7.5초가 된다 — 5초 하드 제약 위반. 첫 홉을 예약선 앞에서 끊어야 성립한다.
    """
    clock = _Clock()
    fake = _FakeSteps(
        _calls_step(_call(1)), _text_step("응.", purpose="tool_final"),
        clock=clock, elapse=2.5,  # step 1이 제 상한(5.0-2.5)을 꽉 채운다
    )
    await _run(fake, registry=_FakeRegistry(_FakeTool()), clock=clock, monkeypatch=monkeypatch)

    step1, step2 = fake.calls[0]["timeout"], fake.calls[-1]["timeout"]
    assert step1 == 2.5
    assert step1 + step2 <= 5.0, f"턴 합계 {step1 + step2}s > 5초 제약"


async def test_tools_not_started_when_remaining_below_final_reserve(monkeypatch):
    """남은 시간 < final_reserve → 도구를 **시작하지 않고** step 2로 직행한다."""
    clock = _Clock()
    tool = _FakeTool()
    fake = _FakeSteps(
        _calls_step(_call(1)), _text_step("그냥 얘기할게.", purpose="tool_final"),
        clock=clock, elapse=3.0,  # step 1이 3초를 먹어 남은 2초 < 예약 2.5초
    )
    turn = await _run(fake, registry=_FakeRegistry(tool), clock=clock, monkeypatch=monkeypatch)

    assert tool.ran == 0  # 도구 미시작
    assert _FakeToolSession.opened == 0  # 세션도 열지 않는다
    assert turn.skipped == "deadline"
    assert [r.error_code for r in turn.tool_results] == ["cancelled"]
    assert turn.text == "그냥 얘기할게."
    assert len(fake.calls) == 2 and fake.calls[1]["tool_choice"] == "none"


async def test_final_call_never_extends_the_absolute_deadline(monkeypatch):
    """상류가 예약선을 어겼어도 최종 호출을 덧붙여 HTTP 절대 마감을 넘기지 않는다."""
    clock = _Clock()
    fake = _FakeSteps(
        _calls_step(_call(1)), _text_step("응.", purpose="tool_final"), clock=clock, elapse=9.0
    )
    with pytest.raises(TimeoutError):
        await _run(fake, registry=_FakeRegistry(_FakeTool()), clock=clock, monkeypatch=monkeypatch)
    assert len(fake.calls) == 1


# --- 3) 도구 실행 결과의 형식 완결 ---
async def test_partial_tool_failure_still_closes_every_call_id(monkeypatch):
    ok = _FakeTool("search_memory")
    bad = _FakeTool("search_diaries", boom=RuntimeError("도구 폭발"))
    fake = _FakeSteps(_calls_step(_call(1), _call(2, "search_diaries")), _text_step("응.", purpose="tool_final"))
    turn = await _run(fake, registry=_FakeRegistry(ok, bad), monkeypatch=monkeypatch)

    assert [r.call_id for r in turn.tool_results] == ["call_1", "call_2"]
    assert [r.status for r in turn.tool_results] == ["ok", "unavailable"]
    assert turn.tool_results[1].error_code == "internal"
    assert turn.tool_results[1].data is None  # unavailable 불변식
    assert turn.text == "응."


async def test_all_tools_failing_still_produces_reply(monkeypatch):
    tools = [
        _FakeTool("search_memory", boom=RuntimeError("x")),
        _FakeTool("search_diaries", boom=RuntimeError("y")),
    ]
    fake = _FakeSteps(
        _calls_step(_call(1), _call(2, "search_diaries")),
        _text_step("그래도 답은 한다.", purpose="tool_final"),
    )
    turn = await _run(fake, registry=_FakeRegistry(*tools), monkeypatch=monkeypatch)

    assert turn.text == "그래도 답은 한다."
    assert all(r.status == "unavailable" for r in turn.tool_results)


async def test_tool_timeout_becomes_unavailable_result(monkeypatch):
    slow = _FakeTool("search_memory", delay=0.2, timeout_ms=10)
    fake = _FakeSteps(_calls_step(_call(1)), _text_step("응.", purpose="tool_final"))
    turn = await _run(fake, registry=_FakeRegistry(slow), monkeypatch=monkeypatch)

    assert turn.tool_results[0].error_code == "timeout"
    assert turn.tool_results[0].status == "unavailable"


async def test_calls_beyond_limit_are_closed_with_tool_call_limit(monkeypatch):
    """앞 3개만 실행하되 4번째 이후도 결과를 붙인다 — 안 붙이면 step 2를 만들 수 없다."""
    tools = [_FakeTool(f"t{i}") for i in range(5)]
    fake = _FakeSteps(
        _calls_step(*(_call(i, f"t{i}") for i in range(5))),
        _text_step("응.", purpose="tool_final"),
    )
    turn = await _run(fake, registry=_FakeRegistry(*tools), monkeypatch=monkeypatch)

    assert sum(t.ran for t in tools) == 3
    assert [r.error_code for r in turn.tool_results[3:]] == ["tool_call_limit", "tool_call_limit"]
    assert [r.call_id for r in turn.tool_results] == [f"call_{i}" for i in range(5)]


async def test_invalid_arguments_are_not_executed(monkeypatch):
    tool = _FakeTool()
    fake = _FakeSteps(
        _calls_step(_call(1, validation_error="malformed_json")),
        _text_step("응.", purpose="tool_final"),
    )
    turn = await _run(fake, registry=_FakeRegistry(tool), monkeypatch=monkeypatch)

    assert tool.ran == 0
    assert turn.tool_results[0].error_code == "invalid_arguments"


async def test_unknown_tool_name_is_closed_without_execution(monkeypatch):
    fake = _FakeSteps(_calls_step(_call(1, "없는도구")), _text_step("응.", purpose="tool_final"))
    turn = await _run(fake, registry=_FakeRegistry(_FakeTool()), monkeypatch=monkeypatch)
    assert turn.tool_results[0].error_code == "invalid_arguments"


# --- 4) 세션 격리(SOMA-374) ---
async def test_each_tool_gets_its_own_short_lived_readonly_session(monkeypatch):
    tools = [_FakeTool("t0"), _FakeTool("t1")]
    fake = _FakeSteps(
        _calls_step(_call(0, "t0"), _call(1, "t1")), _text_step("응.", purpose="tool_final")
    )
    await _run(fake, registry=_FakeRegistry(*tools), monkeypatch=monkeypatch)

    assert _FakeToolSession.opened == 2      # 툴별 1개
    assert _FakeToolSession.closed == 2      # 전부 닫힘
    assert _FakeToolSession.rolled_back == 2  # 커밋 없음(read-only 보장)
    assert tools[0].sessions[0] is not tools[1].sessions[0]


# --- 5) 안전 게이트 ---
async def test_safety_gate_skips_tools(monkeypatch):
    tool = _FakeTool()
    fake = _FakeSteps(_text_step("괜찮아.", purpose="chat"))
    agent_runtime.SAFETY_CLASSIFIER = lambda text: "위기" in text
    turn = await _run(
        fake, registry=_FakeRegistry(tool), user_text="지금 위기야", monkeypatch=monkeypatch
    )

    assert turn.skipped == "safety"
    assert fake.calls[0]["tools"] is None and fake.calls[0]["tool_choice"] == "none"
    assert tool.ran == 0


def test_no_safety_classifier_by_default():
    """새 위기 분류 정책은 제품·법무 승인 전에 넣지 않는다(명세 §5.6) — 기본은 훅만 비어 있다."""
    assert agent_runtime.SAFETY_CLASSIFIER is None


# --- 6) control_intents: final step에서만(명세 487행, W4 검증자 핸드오프) ---
def _intent_step(text: str, *intents: ControlIntent, purpose="tool_final") -> StepResult:
    return StepResult(
        text=text, tool_calls=[], finish_reason="stop", usage=_usage(purpose),
        control_intents=list(intents),
    )


async def test_decide_step_never_receives_control_tools(monkeypatch):
    """첫 홉에 control_tools를 넘기면 애초에 intent가 나온다 — 넘기지 않는 게 판정의 본체다."""
    registry = _FakeRegistry(_FakeTool(), control={"pin_memory": "pin"})
    fake = _FakeSteps(_calls_step(_call(1)), _text_step("응.", purpose="tool_final"))
    await _run(fake, registry=registry, monkeypatch=monkeypatch)

    assert fake.calls[0]["control_tools"] is None            # step 1
    assert fake.calls[1]["control_tools"] == {"pin_memory": "pin"}  # step 2(final)


async def test_control_call_in_decide_step_is_dropped_and_measured(monkeypatch, caplog):
    """첫 홉에서 control 도구를 부르면 실행도 저장도 없이 버리고 계측만 남긴다."""
    caplog.set_level(logging.INFO, logger="moly-backend")
    tool = _FakeTool()
    registry = _FakeRegistry(tool, control={"pin_memory": "pin"})
    fake = _FakeSteps(
        _calls_step(_call(1, "pin_memory"), _call(2)), _text_step("응.", purpose="tool_final")
    )
    turn = await _run(fake, registry=registry, monkeypatch=monkeypatch)

    assert turn.tool_results[0].error_code == "control_intent_ignored"
    assert turn.tool_results[0].status == "unavailable"
    assert turn.tool_results[0].data is None
    assert [r.call_id for r in turn.tool_results] == ["call_1", "call_2"]  # 형식은 닫힌다
    assert tool.ran == 1  # 나머지 정상 도구는 그대로 실행
    shadow = [r for r in caplog.records if "control_intent_shadow" in r.getMessage()]
    assert shadow and '"stage": "decide"' in shadow[0].getMessage()
    assert '"action": "dropped"' in shadow[0].getMessage()


async def test_decide_step_intents_are_discarded_even_if_returned(monkeypatch, caplog):
    """계약상 나올 수 없지만, 나오더라도 버리고 계측만 — AgentTurn에 실려 나가지 않는다."""
    caplog.set_level(logging.WARNING, logger="moly-backend")
    intent = ControlIntent(kind="pin", target_fact_ids=(uuid.uuid4(),))
    fake = _FakeSteps(_intent_step("응.", intent, purpose="tool_decide"))
    turn = await _run(fake, registry=_FakeRegistry(_FakeTool()), monkeypatch=monkeypatch)

    assert turn.control_intents == ()  # 첫 홉 intent는 승계되지 않는다
    assert any("control_intent_shadow" in r.getMessage() for r in caplog.records)


async def test_final_step_intents_are_returned_for_phase2_application(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="moly-backend")
    intent = ControlIntent(kind="forget", target_fact_ids=(uuid.uuid4(), uuid.uuid4()))
    registry = _FakeRegistry(_FakeTool(), control={"forget_memory": "forget"})
    fake = _FakeSteps(_calls_step(_call(1)), _intent_step("알았어.", intent))
    turn = await _run(fake, registry=registry, monkeypatch=monkeypatch)

    assert turn.text == "알았어."
    assert turn.control_intents == (intent,)
    logged = [r.getMessage() for r in caplog.records if "control_intent_observed" in r.getMessage()]
    assert logged and '"stage": "final"' in logged[-1]
    assert '"kind": "forget"' in logged[-1] and '"n_targets": 2' in logged[-1]
    assert str(intent.target_fact_ids[0]) not in logged[-1]  # 대상 id 원문은 남기지 않는다


# --- 7) 카나리 ---
def test_canary_is_deterministic_and_bounded():
    cfg0 = _cfg(canary_pct=0.0)
    cfg100 = _cfg(canary_pct=100.0)
    assert agent_runtime.in_canary(UID, cfg0) is False
    assert agent_runtime.in_canary(UID, cfg100) is True

    cfg = _cfg(canary_pct=1.0)
    first = agent_runtime.in_canary(UID, cfg)
    assert all(agent_runtime.in_canary(UID, cfg) is first for _ in range(5))  # 재계산 안정


def test_canary_matches_spec_formula():
    import hashlib

    uid = uuid.uuid4()
    bucket = int.from_bytes(hashlib.sha256(uid.bytes).digest()[:8]) % 10_000
    for pct in (0.01, 5.0, 37.5, 99.99):
        assert agent_runtime.in_canary(uid, _cfg(canary_pct=pct)) == (bucket < pct * 100)


def test_should_run_requires_both_switch_and_canary():
    assert agent_runtime.should_run(_cfg(enabled=False, canary_pct=100.0), UID) is False
    assert agent_runtime.should_run(_cfg(enabled=True, canary_pct=0.0), UID) is False
    assert agent_runtime.should_run(_cfg(enabled=True, canary_pct=100.0), UID) is True


def test_should_run_bypasses_non_openai_model():
    """대화 모델을 claude-*로 롤백하면(dormant) 도구 루프는 스스로 비킨다 — 500 대신 단발 경로."""
    cfg = _cfg(enabled=True, canary_pct=100.0)
    assert agent_runtime.should_run(cfg, UID, model="claude-sonnet-5") is False
    assert agent_runtime.should_run(cfg, UID, model="gpt-5.6-luna") is True


# --- 7) 도구 결과 턴 합계 예산 ---
def _ok(call_id: str, data) -> ToolResult:
    return ToolResult(call_id=call_id, tool_name="t", status="ok", data=data)


def test_result_budget_keeps_results_that_fit():
    results = [_ok("a", {"text": "짧다"})]
    out = agent_runtime.apply_result_budget(results, 600)
    assert out == results


def test_result_budget_truncates_in_call_order_and_stays_within_budget():
    big = {"text": "가" * 2000}
    out = agent_runtime.apply_result_budget([_ok("a", big), _ok("b", big)], 600)

    assert [r.call_id for r in out] == ["a", "b"]  # 형식은 항상 완결
    assert all(r.truncated for r in out)
    assert len(out[0].data["text"]) < 2000
    assert sum(agent_runtime._cost(r) for r in out) <= 600  # 턴 **합계** 예산


def test_result_budget_drops_tail_when_no_room_left():
    """앞 결과가 예산을 다 쓰면 뒤 결과는 unavailable로 닫는다(형식은 유지)."""
    out = agent_runtime.apply_result_budget(
        [_ok("a", {"text": "가" * 60}), _ok("b", {"text": "가" * 60})], 70
    )
    assert out[0].status == "ok"
    assert out[1].status == "unavailable" and out[1].error_code == "budget_exceeded"
    assert out[1].data is None and out[1].call_id == "b"


def test_result_budget_zero_closes_everything_without_data():
    out = agent_runtime.apply_result_budget([_ok("a", {"text": "가" * 100})], 0)
    assert out[0].status == "unavailable" and out[0].data is None


async def test_budget_applied_before_step2(monkeypatch):
    tool = _FakeTool(data={"text": "가" * 5000})
    fake = _FakeSteps(_calls_step(_call(1)), _text_step("응.", purpose="tool_final"))
    turn = await _run(
        fake, registry=_FakeRegistry(tool), cfg=_cfg(tool_result_budget_tokens=50),
        monkeypatch=monkeypatch,
    )
    sent = [i for i in fake.calls[1]["transcript"] if isinstance(i, ToolResult)]
    assert sent and sent[0].truncated is True
    assert turn.tool_results[0].truncated is True


# --- 8) chat 배선 ---
async def _post(session, monkeypatch, *, reply="응.", agent=None, snapshot=None):
    async def _res(s, user_id):
        return _gating()

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text=reply, input_tokens=10, output_tokens=20, model="gpt-5.6-luna")

    async def _fake_cfg(s):
        return snapshot if snapshot is not None else build_snapshot({})

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    monkeypatch.setattr(agent_config, "effective_agent_config", _fake_cfg)
    if agent is not None:
        monkeypatch.setattr(agent_runtime, "run_turn", agent)
    req = SimpleNamespace(text="오늘 어땠어?", greeting_id=None)
    return await chat_service.post_message(session, str(UID), req, "idem-agent")


async def test_disabled_agent_keeps_single_shot_path(monkeypatch):
    """기본값(agent_enabled=False)이면 루프는 호출조차 되지 않는다 — 기존 경로 그대로."""
    assert chat_service.settings.agent_enabled is False
    ran = []

    async def _never(*a, **kw):
        ran.append(kw)
        raise AssertionError("도구 루프가 꺼져 있어야 한다")

    out = await _post(FakeSession(), monkeypatch, reply="그냥 그랬어.", agent=_never)
    assert out.reply.content == "그냥 그랬어."
    assert ran == []


async def test_enabled_agent_reply_and_usage_come_from_loop(monkeypatch):
    captured = {}

    async def _agent(system, convo, **kw):
        captured.update(kw)
        return agent_runtime.AgentTurn(
            text="도구 써서 답했어.", calls=[_usage("tool_decide"), _usage("tool_final")]
        )

    snapshot = build_snapshot({"agent_enabled": True, "agent_canary_pct": 100.0})
    out = await _post(FakeSession(), monkeypatch, agent=_agent, snapshot=snapshot)

    assert out.reply.content == "도구 써서 답했어."
    assert out.tokens_used == 1000 + 80  # 두 호출의 billable 합계가 그대로 차감된다
    assert captured["user_text"] == "오늘 어땠어?"
    assert captured["user_id"] == UID
    assert captured["language"] == "ko"


async def test_canary_zero_keeps_single_shot_path(monkeypatch):
    async def _never(*a, **kw):
        raise AssertionError("카나리 0%면 루프를 타면 안 된다")

    snapshot = build_snapshot({"agent_enabled": True, "agent_canary_pct": 0.0})
    out = await _post(FakeSession(), monkeypatch, reply="응.", agent=_never, snapshot=snapshot)
    assert out.reply.content == "응."


async def test_agent_turn_still_runs_egress_backstops(monkeypatch):
    """도구 턴도 egress 순서를 그대로 탄다(정제·placeholder). 새 경로가 백스톱을 건너뛰면 회귀다."""

    async def _agent(system, convo, **kw):
        return agent_runtime.AgentTurn(text="무슨 일인데... 지훈아", calls=[_usage("tool_final")])

    snapshot = build_snapshot({"agent_enabled": True, "agent_canary_pct": 100.0})
    out = await _post(FakeSession(), monkeypatch, agent=_agent, snapshot=snapshot)

    assert "..." not in out.reply.content  # 말줄임표 제거
    assert out.reply.content.endswith("?")  # 되묻기 물음표 복원


async def test_config_db_failure_propagates_and_saves_nothing(monkeypatch):
    """설정 조회 실패를 삼키면 설정 장애가 조용히 기본값으로 둔갑한다 — Phase 1 DB 오류로 올린다."""
    called = []

    async def _boom(session):
        raise RuntimeError("app_config 조회 실패")

    async def _llm_must_not_run(*a, **kw):
        called.append(1)
        raise AssertionError("Phase 1에서 죽었어야 한다")

    monkeypatch.setattr(agent_config, "effective_agent_config", _boom)
    monkeypatch.setattr(gating_module, "resolve", lambda s, u: _gating())
    session = FakeSession()

    async def _res(s, user_id):
        return _gating()

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _llm_must_not_run)
    req = SimpleNamespace(text="오늘 어땠어?", greeting_id=None)

    with pytest.raises(RuntimeError):
        await chat_service.post_message(session, str(UID), req, "idem-cfg-fail")
    assert called == [] and session.added == []  # LLM도 저장도 없다(클린 재시도)


async def test_control_intents_are_applied_without_persisting_internal_rows(monkeypatch):
    """기억 제어는 적용하되 내부 control intent 자체를 메시지로 저장하지 않는다."""
    from app.models.idempotency_key import IdempotencyKey
    from app.models.message import Message

    intent = ControlIntent(kind="forget", target_fact_ids=(uuid.uuid4(),))

    async def _agent(system, convo, **kw):
        return agent_runtime.AgentTurn(
            text="알았어.", calls=[_usage("tool_final")], control_intents=(intent,)
        )

    session = FakeSession()
    snapshot = build_snapshot({"agent_enabled": True, "agent_canary_pct": 100.0})
    out = await _post(session, monkeypatch, agent=_agent, snapshot=snapshot)

    assert out.reply.content == "내가 기억하고 있던 것에서는 찾지 못했어."
    # 저장된 건 유저 메시지·캐피 응답·멱등 응답뿐이다(기억 제어 행 없음).
    assert [type(o) for o in session.added] == [Message, Message, IdempotencyKey]
    assert all("forget" not in (m.content or "") for m in session.added if isinstance(m, Message))


async def test_metrics_mark_tool_turn(monkeypatch, caplog):
    from tests.test_chat_metrics import _turn_metrics_payload

    async def _agent(system, convo, **kw):
        return agent_runtime.AgentTurn(text="응.", calls=[_usage("tool_decide"), _usage("tool_final")])

    caplog.set_level(logging.INFO, logger="moly-backend")
    snapshot = build_snapshot({"agent_enabled": True, "agent_canary_pct": 100.0})
    await _post(FakeSession(), monkeypatch, agent=_agent, snapshot=snapshot)
    assert _turn_metrics_payload(caplog)["used_tools"] is True

"""W4 — llm.generate_step() 툴콜 계약.

핵심 회귀: 툴콜 응답(content=None)을 빈 답변으로 오인하지 않는다 / 미완결 transcript로는
모델 호출을 만들지 않는다 / tool_choice="none"이 강제된다 / Anthropic은 명시적 오류.
기존 generate()는 무변경(시그니처·동작 회귀 테스트 포함).
"""
import inspect
import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from app.services import chat as c
from app.services import llm as m
from app.services.llm import (
    AssistantText,
    AssistantToolCalls,
    ControlIntent,
    LLMResult,
    ToolCall,
    ToolContractError,
    ToolResult,
    UnsupportedProviderError,
    UserText,
)

MODEL = "gpt-5.6-luna"


# --- fake OpenAI client ---
class _FakeCompletions:
    def __init__(self, resp):
        self._resp = resp
        self.calls = 0
        self.kw = None

    async def create(self, **kw):
        self.calls += 1
        self.kw = kw
        return self._resp


class _FakeClient:
    def __init__(self, resp):
        self.completions = _FakeCompletions(resp)
        self.chat = SimpleNamespace(completions=self.completions)


def _usage(prompt=100, completion=10, cached=0):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _wire_call(call_id, name, args):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=args),
    )


def _resp(content=None, tool_calls=None, finish_reason="stop", usage=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage if usage is not None else _usage(),
    )


def _install(monkeypatch, resp):
    client = _FakeClient(resp)
    monkeypatch.setattr(m, "_get_openai_client", lambda: client)
    return client


TOOLS = [
    {"type": "function", "function": {"name": "get_routines", "parameters": {"type": "object"}}},
    {"type": "function", "function": {"name": "get_diary", "parameters": {"type": "object"}}},
]


async def _step(monkeypatch, resp, **over):
    client = _install(monkeypatch, resp)
    kw = dict(tools=TOOLS, tool_choice="auto", model=MODEL, max_tokens=192, timeout=3.0)
    kw.update(over)
    return await m.generate_step("페르소나", [UserText("오늘 루틴 뭐였지")], **kw), client


# =====================================================================================
# 1) content=None을 빈 답변으로 처리하지 않는다 (이 작업의 핵심 버그)
# =====================================================================================
async def test_tool_call_response_is_not_treated_as_empty_reply(monkeypatch):
    resp = _resp(
        content=None,
        tool_calls=[_wire_call("call_1", "get_routines", '{"date":"2026-08-03"}')],
        finish_reason="tool_calls",
    )
    step, _ = await _step(monkeypatch, resp)
    assert step.text is None  # "" 로 뭉개지 않는다 — 텍스트 없음과 빈 답변은 다르다
    assert step.finish_reason == "tool_calls"
    assert [(t.call_id, t.tool_name, t.arguments) for t in step.tool_calls] == [
        ("call_1", "get_routines", {"date": "2026-08-03"})
    ]
    assert step.tool_calls[0].validation_error is None


async def test_same_response_through_legacy_generate_would_be_empty(monkeypatch):
    """대조군: 기존 generate()로 같은 응답을 받으면 빈 문자열이 된다(=오인). generate_step이 이걸 고친다."""
    resp = _resp(content=None, tool_calls=[_wire_call("call_1", "get_routines", "{}")])
    _install(monkeypatch, resp)
    r = await m.generate("p", [{"role": "user", "content": "hi"}], model=MODEL)
    assert r.text == ""  # 기존 동작(무변경) — 그래서 generate_step이 따로 필요하다


async def test_text_only_response_preserved(monkeypatch):
    step, _ = await _step(monkeypatch, _resp(content="응 알았어", tool_calls=None))
    assert step.text == "응 알았어" and step.tool_calls == []


async def test_empty_string_content_stays_empty_string(monkeypatch):
    """빈 문자열은 None으로 바꾸지 않는다(두 상태를 계속 구분)."""
    step, _ = await _step(monkeypatch, _resp(content="", tool_calls=None))
    assert step.text == ""


async def test_no_choices_is_safe(monkeypatch):
    resp = SimpleNamespace(choices=[], usage=_usage())
    _install(monkeypatch, resp)
    step = await m.generate_step(
        "p", [UserText("hi")], tools=None, tool_choice="auto",
        model=MODEL, max_tokens=64, timeout=1.0,
    )
    assert step.text is None and step.tool_calls == [] and step.finish_reason == ""


# =====================================================================================
# 2) tool_choice="none" 강제
# =====================================================================================
async def test_tool_choice_none_is_sent_on_wire(monkeypatch):
    step, client = await _step(monkeypatch, _resp(content="끝"), tool_choice="none")
    assert client.completions.kw["tool_choice"] == "none"
    assert step.text == "끝"


async def test_tool_choice_none_drops_returned_tool_calls(monkeypatch):
    """최종 step에서 모델이 그래도 도구를 부르면 버린다 — 예약 예산 밖의 추가 라운드를 만들지 않는다."""
    resp = _resp(
        content="그래도 부를래",
        tool_calls=[_wire_call("call_9", "get_diary", "{}")],
        finish_reason="tool_calls",
    )
    step, _ = await _step(monkeypatch, resp, tool_choice="none")
    assert step.tool_calls == []
    assert step.text == "그래도 부를래"


async def test_tools_none_omits_tool_keys(monkeypatch):
    _, client = await _step(monkeypatch, _resp(content="x"), tools=None)
    assert "tools" not in client.completions.kw and "tool_choice" not in client.completions.kw
    assert client.completions.kw["max_completion_tokens"] == 192  # max_tokens 아님(GPT-5.x)
    assert client.completions.kw["reasoning_effort"] == "none"


async def test_function_tools_disable_reasoning_on_chat_completions(monkeypatch):
    """GPT-5.6 Chat Completions는 function tools+reasoning 조합을 400으로 거부한다."""
    _, client = await _step(monkeypatch, _resp(content="x"))
    assert client.completions.kw["tools"] == TOOLS
    assert client.completions.kw["reasoning_effort"] == "none"


# =====================================================================================
# 3) 형식 완결성 — 결과가 안 붙은 call이 있으면 다음 호출을 만들지 않는다
# =====================================================================================
def _two_calls():
    return AssistantToolCalls(
        calls=(ToolCall("c1", "get_routines", {}), ToolCall("c2", "get_diary", {"date": "2026-08-03"}))
    )


def _ok(call_id, name, data=None):
    return ToolResult(call_id=call_id, tool_name=name, status="ok", data=data or {})


async def test_missing_tool_result_makes_no_api_call(monkeypatch):
    client = _install(monkeypatch, _resp(content="x"))
    transcript = [UserText("hi"), _two_calls(), _ok("c1", "get_routines")]
    with pytest.raises(ToolContractError, match="c2"):
        await m.generate_step(
            "p", transcript, tools=TOOLS, tool_choice="none",
            model=MODEL, max_tokens=64, timeout=1.0,
        )
    assert client.completions.calls == 0  # 호출 자체를 만들지 않는다


async def test_all_results_present_makes_the_call(monkeypatch):
    client = _install(monkeypatch, _resp(content="정리했어"))
    transcript = [UserText("hi"), _two_calls(), _ok("c1", "get_routines"), _ok("c2", "get_diary")]
    step = await m.generate_step(
        "p", transcript, tools=TOOLS, tool_choice="none",
        model=MODEL, max_tokens=64, timeout=1.0,
    )
    assert client.completions.calls == 1 and step.text == "정리했어"
    roles = [msg["role"] for msg in client.completions.kw["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "tool"]


def test_tool_result_order_must_match_call_order():
    transcript = [_two_calls(), _ok("c2", "get_diary"), _ok("c1", "get_routines")]
    with pytest.raises(ToolContractError, match="순서 불일치"):
        m.to_openai_messages("p", transcript)


def test_orphan_tool_result_rejected():
    with pytest.raises(ToolContractError, match="짝 없는"):
        m.to_openai_messages("p", [UserText("hi"), _ok("c1", "get_routines")])


def test_duplicate_call_ids_rejected():
    dup = AssistantToolCalls(calls=(ToolCall("c1", "get_diary", {}), ToolCall("c1", "get_diary", {})))
    with pytest.raises(ToolContractError, match="중복"):
        m.to_openai_messages("p", [dup, _ok("c1", "get_diary"), _ok("c1", "get_diary")])


def test_tool_name_mismatch_rejected():
    with pytest.raises(ToolContractError, match="도구명 불일치"):
        m.to_openai_messages("p", [_two_calls(), _ok("c1", "get_diary"), _ok("c2", "get_diary")])


def test_trailing_unclosed_calls_rejected():
    with pytest.raises(ToolContractError, match="결과가 없는"):
        m.to_openai_messages("p", [_two_calls()])


def test_empty_tool_calls_message_rejected():
    with pytest.raises(ToolContractError, match="빈 AssistantToolCalls"):
        m.to_openai_messages("p", [AssistantToolCalls(calls=())])


# =====================================================================================
# 4) Anthropic 모델 = 명시적 오류(dormant)
# =====================================================================================
async def test_anthropic_model_raises(monkeypatch):
    _install(monkeypatch, _resp(content="x"))
    with pytest.raises(UnsupportedProviderError, match="claude-sonnet-5"):
        await m.generate_step(
            "p", [UserText("hi")], tools=TOOLS, tool_choice="auto",
            model="claude-sonnet-5", max_tokens=64, timeout=1.0,
        )


async def test_empty_model_also_raises(monkeypatch):
    """model 미지정("")은 provider_for 상 anthropic → 조용히 OpenAI로 새지 않는다."""
    _install(monkeypatch, _resp(content="x"))
    with pytest.raises(UnsupportedProviderError):
        await m.generate_step(
            "p", [UserText("hi")], tools=None, tool_choice="auto",
            model="", max_tokens=64, timeout=1.0,
        )


# =====================================================================================
# 5) wire 변환 — 명세 표 그대로 + 왕복 대칭
# =====================================================================================
def test_wire_shapes_match_spec():
    call = ToolCall("c1", "get_diary", {"b": "값", "a": 1})
    result = ToolResult(
        call_id="c1", tool_name="get_diary", status="ok", data={"text": "안녕"},
        truncated=True, duration_ms=123,
    )
    msgs = m.to_openai_messages(["페르소나", "", "기억"], [UserText("u"), AssistantText("a"),
                                                        AssistantToolCalls((call,)), result])
    assert msgs[0] == {"role": "system", "content": "페르소나\n\n기억"}  # 빈 블록 제거
    assert msgs[1] == {"role": "user", "content": "u"}
    assert msgs[2] == {"role": "assistant", "content": "a"}
    assert msgs[3] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                # sort_keys=True·ensure_ascii=False·compact
                "function": {"name": "get_diary", "arguments": '{"a":1,"b":"값"}'},
            }
        ],
    }
    assert msgs[4]["role"] == "tool" and msgs[4]["tool_call_id"] == "c1"
    assert msgs[4]["content"] == (
        '{"schema_version":1,"tool":"get_diary","status":"ok","data":{"text":"안녕"},'
        '"error_code":null,"truncated":true}'
    )
    assert "duration_ms" not in msgs[4]["content"]  # trace 전용 — 모델 wire에 넣지 않는다


def test_wire_roundtrip_is_symmetric():
    transcript = [
        UserText("오늘 뭐했더라"),
        AssistantText("글쎄"),
        AssistantToolCalls((ToolCall("c1", "get_routines", {"date": "2026-08-03"}),
                            ToolCall("c2", "get_diary", {}))),
        ToolResult("c1", "get_routines", "ok", [{"name": "산책"}], truncated=True),
        ToolResult("c2", "get_diary", "unavailable", None, error_code="timeout"),
        AssistantText("정리해줄게"),
    ]
    msgs = m.to_openai_messages("페르소나", transcript)
    system, back = m.from_openai_messages(msgs)
    assert system == "페르소나"
    assert back == transcript  # duration_ms=0 기본값까지 동일(왕복 대칭)
    assert m.to_openai_messages(system, back) == msgs  # wire → typed → wire 도 동일


def test_json_dump_is_deterministic_for_cache():
    a = m.to_openai_messages("p", [AssistantToolCalls((ToolCall("c", "t", {"z": 1, "a": 2}),)),
                                   _ok("c", "t")])
    b = m.to_openai_messages("p", [AssistantToolCalls((ToolCall("c", "t", {"a": 2, "z": 1}),)),
                                   _ok("c", "t")])
    assert a == b  # 인자 키 순서가 달라도 같은 wire = 프리픽스 캐시 유지


# =====================================================================================
# 6) 불변식 — unavailable/ok
# =====================================================================================
def test_unavailable_with_data_rejected():
    bad = ToolResult("c1", "get_diary", "unavailable", {"x": 1}, error_code="timeout")
    with pytest.raises(ToolContractError, match="data"):
        m.to_openai_messages("p", [AssistantToolCalls((ToolCall("c1", "get_diary", {}),)), bad])


def test_unavailable_without_error_code_rejected():
    bad = ToolResult("c1", "get_diary", "unavailable", None)
    with pytest.raises(ToolContractError, match="error_code가 없다"):
        m.to_openai_messages("p", [AssistantToolCalls((ToolCall("c1", "get_diary", {}),)), bad])


def test_ok_with_error_code_rejected():
    bad = ToolResult("c1", "get_diary", "ok", {}, error_code="timeout")
    with pytest.raises(ToolContractError, match="ok 결과"):
        m.to_openai_messages("p", [AssistantToolCalls((ToolCall("c1", "get_diary", {}),)), bad])


def test_unavailable_result_helper_closes_the_form():
    r = m.unavailable_result(ToolCall("c1", "get_diary", {}, validation_error="malformed_json"))
    assert (r.status, r.data, r.error_code, r.schema_version) == ("unavailable", None, "invalid_arguments", 1)
    m.to_openai_messages("p", [AssistantToolCalls((ToolCall("c1", "get_diary", {}),)), r])  # 형식 통과


def test_unavailable_result_requires_error_code():
    with pytest.raises(ToolContractError):
        m.unavailable_result(ToolCall("c1", "t", {}), error_code="")


# =====================================================================================
# 7) 인자 검증 실패는 실행하지 않고 형식만 닫는다
# =====================================================================================
class _DiaryArgs(BaseModel):
    date: str


async def test_malformed_json_arguments_marked_not_executed(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call("c1", "get_diary", "{not json")])
    step, _ = await _step(monkeypatch, resp)
    call = step.tool_calls[0]
    assert call.validation_error == "malformed_json" and call.arguments == {}
    assert m.unavailable_result(call).error_code == "invalid_arguments"


async def test_unknown_tool_name_marked(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call("c1", "drop_table", "{}")])
    step, _ = await _step(monkeypatch, resp)
    assert step.tool_calls[0].validation_error == "unknown_tool"


async def test_pydantic_mismatch_marked(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call("c1", "get_diary", '{"wrong":1}')])
    step, _ = await _step(monkeypatch, resp, input_models={"get_diary": _DiaryArgs})
    assert step.tool_calls[0].validation_error == "schema_mismatch"


async def test_pydantic_validated_arguments_are_normalized(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call("c1", "get_diary", '{"date":"2026-08-03","x":9}')])
    step, _ = await _step(monkeypatch, resp, input_models={"get_diary": _DiaryArgs})
    call = step.tool_calls[0]
    assert call.validation_error is None
    assert call.arguments == {"date": "2026-08-03"}  # 모델 밖 필드는 떨어진다
    json.dumps(call.arguments)  # 검증 완료 값은 항상 JSON 직렬화 가능


async def test_non_object_arguments_marked(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call("c1", "get_diary", "[1,2]")])
    step, _ = await _step(monkeypatch, resp)
    assert step.tool_calls[0].validation_error == "malformed_json"


async def test_missing_call_id_marked(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call(None, "get_diary", "{}")])
    step, _ = await _step(monkeypatch, resp)
    assert step.tool_calls[0].validation_error == "malformed_call"


# =====================================================================================
# 8) usage = W1의 LlmCall (TurnUsage 누적 가능)
# =====================================================================================
async def test_usage_is_llm_call_and_accumulates(monkeypatch):
    step, _ = await _step(monkeypatch, _resp(content="x", usage=_usage(5000, 100, 4000)))
    u = step.usage
    assert isinstance(u, m.LlmCall)
    assert (u.provider, u.model, u.purpose) == ("openai", MODEL, "tool_decide")
    assert (u.input_tokens, u.cache_read_tokens, u.cache_write_tokens) == (0, 4000, 1000)
    turn = c.TurnUsage()
    turn.calls.append(u)
    assert turn.total_billable == u.billable
    assert turn.totals["output_tokens"] == 100


async def test_usage_purpose_overridable(monkeypatch):
    step, _ = await _step(monkeypatch, _resp(content="x"), purpose="tool_final")
    assert step.usage.purpose == "tool_final"


async def test_usage_none_is_safe(monkeypatch):
    resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="x", tool_calls=None),
                                 finish_reason="stop")],
        usage=None,
    )
    _install(monkeypatch, resp)
    step = await m.generate_step(
        "p", [UserText("hi")], tools=None, tool_choice="auto",
        model=MODEL, max_tokens=64, timeout=1.0,
    )
    assert step.usage.billable == 0 and step.usage.input_tokens == 0


@pytest.mark.parametrize(
    "prompt,completion,cached",
    [(5000, 100, 4000), (1000, 50, 800), (1023, 10, 0), (1024, 10, 0), (4000, 20, 4000)],
)
async def test_step_usage_matches_generate_usage(monkeypatch, prompt, completion, cached):
    """generate_step의 버킷 배분·billable이 기존 generate() 경로와 완전히 같은지(회계 드리프트 방지)."""
    _install(monkeypatch, _resp(content="x", usage=_usage(prompt, completion, cached)))
    step = await m.generate_step(
        "p", [UserText("hi")], tools=None, tool_choice="auto",
        model=MODEL, max_tokens=64, timeout=1.0,
    )
    _install(monkeypatch, _resp(content="x", usage=_usage(prompt, completion, cached)))
    legacy = await m.generate("p", [{"role": "user", "content": "hi"}], model=MODEL)
    assert (step.usage.input_tokens, step.usage.output_tokens,
            step.usage.cache_read_tokens, step.usage.cache_write_tokens) == (
        legacy.input_tokens, legacy.output_tokens,
        legacy.cache_read_tokens, legacy.cache_write_tokens,
    )
    assert step.usage.billable == c._billable(legacy)  # 회계 소유자(chat)와 등가


@pytest.mark.parametrize("model", [MODEL, "claude-sonnet-5", ""])
def test_billable_tokens_equals_chat_billable(model):
    """llm.billable_tokens ↔ chat._billable 등가 고정 — 한쪽 가중치만 바뀌면 여기서 깨진다."""
    r = LLMResult("t", input_tokens=137, output_tokens=91, cache_read_tokens=3001,
                  cache_write_tokens=777, model=model)
    assert m.billable_tokens(r) == c._billable(r)


# =====================================================================================
# 9) control_intents — 호출측 Phase 2 전달 전용
# =====================================================================================
CONTROL = {"pin_fact": "pin", "forget_fact": "forget"}


async def test_control_intent_parsed_and_excluded_from_tool_calls(monkeypatch):
    fid = str(uuid.uuid4())
    resp = _resp(
        content="기억해둘게",
        tool_calls=[
            _wire_call("c1", "pin_fact", json.dumps({"target_fact_ids": [fid], "value": "커피"})),
            _wire_call("c2", "get_diary", "{}"),
        ],
    )
    step, _ = await _step(monkeypatch, resp, tool_choice="none", control_tools=CONTROL)
    assert step.control_intents == [
        ControlIntent(kind="pin", target_fact_ids=(uuid.UUID(fid),), value="커피")
    ]
    assert step.tool_calls == []  # 이 계층의 실행 대상이 아니며 호출측 Phase 2가 적용한다


async def test_control_intents_empty_without_control_tools(monkeypatch):
    """control_tools를 안 주면 어떤 도구도 intent로 해석하지 않는다(자유문 정규식 보충 없음)."""
    resp = _resp(content="이건 꼭 기억해줘 라고 말해도", tool_calls=None)
    step, _ = await _step(monkeypatch, resp)
    assert step.control_intents == []


async def test_control_intent_invalid_uuid_dropped(monkeypatch):
    resp = _resp(content=None, tool_calls=[
        _wire_call("c1", "forget_fact", '{"target_fact_ids":["not-a-uuid"]}')
    ])
    step, _ = await _step(monkeypatch, resp, tool_choice="none", control_tools=CONTROL)
    assert step.control_intents == [] and step.tool_calls == []


async def test_control_intent_unknown_kind_dropped(monkeypatch):
    resp = _resp(content=None, tool_calls=[_wire_call("c1", "pin_fact", "{}")])
    step, _ = await _step(monkeypatch, resp, tool_choice="none",
                          control_tools={"pin_fact": "delete_everything"})
    assert step.control_intents == []


# =====================================================================================
# 10) 기존 generate() 회귀 — 시그니처·동작 무변경
# =====================================================================================
def test_generate_signature_unchanged():
    """기존 인자·기본값은 그대로. 추가는 기본값 있는 keyword-only만 허용한다.

    호출부가 수십 곳(워커·일기·복원·도구)이라 기존 인자가 바뀌면 조용히 깨진다. 반면 계측용
    `ledger`처럼 기본값이 있는 옵션 추가는 기존 호출을 바꾸지 않으므로 허용한다.
    """
    sig = inspect.signature(m.generate)
    original = [
        "system", "convo", "max_tokens", "model",
        "cache_messages", "ttl_system", "ttl_messages", "timeout",
    ]
    params = list(sig.parameters)
    assert params[: len(original)] == original  # 기존 인자는 순서까지 그대로
    defaults = {k: p.default for k, p in sig.parameters.items() if p.default is not p.empty}
    for k, v in {
        "max_tokens": None, "model": None, "cache_messages": False,
        "ttl_system": "5m", "ttl_messages": "5m", "timeout": None,
    }.items():
        assert defaults[k] == v
    # 새로 붙은 인자는 전부 keyword-only + 기본값 보유(= 기존 호출 무영향).
    for name in params[len(original):]:
        p = sig.parameters[name]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is not inspect.Parameter.empty


async def test_generate_still_returns_llm_result(monkeypatch):
    _install(monkeypatch, _resp(content="안녕", usage=_usage(1000, 50, 800)))
    r = await m.generate("페르소나", [{"role": "user", "content": "hi"}], model=MODEL)
    assert isinstance(r, LLMResult)
    assert (r.text, r.input_tokens, r.output_tokens, r.cache_read_tokens) == ("안녕", 200, 50, 800)

"""LLM 래퍼 — 비스트리밍(완성본 반환) + 토큰 usage.

provider는 model-id 프리픽스로 라우팅한다: gpt-* → OpenAI, 그 외(claude-*) → Anthropic.
generate() 시그니처는 provider 무관하게 고정 — 호출부(chat·diary)는 provider를 몰라도 된다.
model 을 claude-* 로 되돌리면 _generate_anthropic 경로로 즉시 복귀(롤백·재사용).

대화는 HTTP 요청-응답 완성본(ARCHITECTURE). 스트리밍 없음.
토큰 집계 = 모델 실측 usage. Anthropic은 system prefix(페르소나+기억) 캐싱, OpenAI는 자동캐시.

계약은 둘이다: 텍스트 in/텍스트 out인 generate()(워커·일기·한자복원)와, 도구 호출을 표현하는
generate_step()(파일 하단 W4 절, OpenAI 전용). 후자를 추가해도 전자는 무변경이다.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from math import ceil
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.config import settings

_log = logging.getLogger(__name__)

_client = None
_openai_client = None


def provider_for(model: str | None) -> str:
    """model-id 프리픽스로 provider 판정. gpt-* → openai, 그 외 → anthropic(claude-*)."""
    return "openai" if (model or "").startswith("gpt-") else "anthropic"


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


def _get_openai_client():
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _openai_client


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0   # 캐시에서 읽음(0.1× 실원가) — 기본 0이라 기존 positional 생성 호환
    cache_write_tokens: int = 0  # 캐시에 씀(1.25× 실원가). Anthropic=실측 / OpenAI=추정(_generate_openai)
    model: str = ""              # 실제 호출 모델 — _billable이 이 prefix로 provider별 가중치 선택


@dataclass
class LlmCall:
    """턴 내 LLM 호출 1건의 회계 단위 — 호출별 usage + 그 호출 단독 billable.

    한 턴이 LLM을 여러 번 부른다(주 chat + 한자 복원, 이후 도구 루프). 청구는 호출별로
    계산해 합산해야 provider·model이 섞여도(주=chat 모델, 복원=utility 모델) 가중치가 맞는다.
    billable 계산은 회계 소유자인 chat._billable이 하고 여기엔 결과만 담는다(순환 import 회피).
    """
    provider: str          # "openai" | "anthropic"
    model: str
    purpose: str           # "chat" | "tool_decide" | "tool_final" | "foreign_repair"
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    billable: int


# OpenAI 자동 프리픽스 캐시가 걸리는 최소 프롬프트 길이. 이 밑이면 캐시 자체가 없다(전량 일반 입력).
_OPENAI_CACHE_MIN_PREFIX_TOKENS = 1024


def billable_tokens(r: LLMResult) -> int:
    """실비용 가중 청구 토큰. chat._billable과 **같은 식**이며 등가는 테스트로 고정한다.

    회계 소유자는 chat이지만(순환 import 회피) generate_step은 LlmCall.billable을 채워 반환해야 해서
    여기에도 같은 계산이 필요하다. 가중치가 갈라지면 tests/test_llm_step.py의 등가 테스트가 깨진다.
    """
    if provider_for(r.model) == "openai":
        w_out = settings.bill_weight_output_openai
        w_read = settings.bill_weight_cache_read_openai
        w_write = settings.bill_weight_cache_write_openai
    else:
        w_out = settings.bill_weight_output
        w_read = settings.bill_weight_cache_read
        w_write = settings.bill_weight_cache_write
    return ceil(
        r.input_tokens
        + w_out * r.output_tokens
        + w_read * r.cache_read_tokens
        + w_write * r.cache_write_tokens
    )


def _cc(ttl: str) -> dict:
    """cache_control 블록. 기본 5m. '1h'면 write 2×(워밍률 측정 후에만)."""
    return {"type": "ephemeral"} if ttl != "1h" else {"type": "ephemeral", "ttl": "1h"}


def _system_blocks(system: str | list[str], ttl: str) -> list[dict]:
    """system을 텍스트 블록 리스트로. 각 블록에 cache_control(각각 breakpoint).

    리스트로 주면 [페르소나(불변), 닉네임+기억(가변)] → 뒤 블록이 바뀌어도 앞(페르소나) 캐시 생존.
    """
    blocks = [system] if isinstance(system, str) else list(system)
    return [{"type": "text", "text": b, "cache_control": _cc(ttl)} for b in blocks if b]


def _cache_last(convo: list[dict], ttl: str) -> list[dict]:
    """마지막 메시지 content를 블록형으로 바꿔 cache_control 부착(증분만 write, 이후 read)."""
    if not convo:
        return convo
    out = [dict(m) for m in convo]
    out[-1] = {
        **out[-1],
        "content": [{"type": "text", "text": out[-1]["content"], "cache_control": _cc(ttl)}],
    }
    return out


def _timeout(timeout: float | None) -> float:
    """per-request 타임아웃(초). None이면 config 기본. SDK 기본(수 분)을 유한값으로 대체."""
    return settings.llm_timeout_s if timeout is None else timeout


async def generate(
    system: str | list[str],
    convo: list[dict],
    *,
    max_tokens: int | None = None,
    model: str | None = None,
    cache_messages: bool = False,
    ttl_system: str = "5m",
    ttl_messages: str = "5m",
    timeout: float | None = None,
) -> LLMResult:
    """system(페르소나+기억) + convo(user/assistant) → 응답 텍스트 + 실측 토큰.

    model 미지정 = 대화 모델(settings.model_chat). 일기 self-check·기억통합은 utility 지정.
    provider는 model 프리픽스로 자동 분기. cache_messages/ttl_* 는 Anthropic 전용(OpenAI 자동캐시).
    timeout 미지정 = settings.llm_timeout_s. 초과 시 SDK가 APITimeoutError를 던진다(호출측이 처리).
    """
    model = model or settings.model_chat
    if provider_for(model) == "openai":
        return await _generate_openai(
            system, convo, model=model, max_tokens=max_tokens, timeout=timeout
        )
    return await _generate_anthropic(
        system, convo, model=model, max_tokens=max_tokens,
        cache_messages=cache_messages, ttl_system=ttl_system, ttl_messages=ttl_messages,
        timeout=timeout,
    )


async def _generate_anthropic(
    system: str | list[str],
    convo: list[dict],
    *,
    model: str,
    max_tokens: int | None = None,
    cache_messages: bool = False,
    ttl_system: str = "5m",
    ttl_messages: str = "5m",
    timeout: float | None = None,
) -> LLMResult:
    """Anthropic 경로(보존). system 리스트면 블록별 breakpoint. cache_messages=True면 마지막 메시지도 캐싱."""
    messages = _cache_last(convo, ttl_messages) if cache_messages else convo
    resp = await _get_client().messages.create(
        model=model,
        max_tokens=max_tokens or settings.llm_max_tokens,
        system=_system_blocks(system, ttl_system),
        messages=messages,
        timeout=_timeout(timeout),
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    u = resp.usage
    return LLMResult(
        text=text,
        # None-safe: 캐시 미참여/게이트웨이/SDK 변경 시 None → ceil(None) 500·메시지 유실 방지.
        input_tokens=getattr(u, "input_tokens", None) or 0,
        output_tokens=getattr(u, "output_tokens", None) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
        model=model,
    )


async def _generate_openai(
    system: str | list[str],
    convo: list[dict],
    *,
    model: str,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> LLMResult:
    """OpenAI 경로(신설). system(str|list) → messages[0] system 합침(평문, cache_control 미부착).

    OpenAI는 프리픽스 자동캐시라 cache_control/ttl 불필요. usage 정규화(이중계상 방지):
    prompt_tokens를 캐시읽기/캐시쓰기/일반입력 3버킷에 **배타** 배분 / output=completion.
    방어: usage None·choices 빈·content None 에도 500 없이 빈 결과로 폴백(응답을 막지 않음).
    """
    sys = system if isinstance(system, str) else "\n\n".join(b for b in system if b)
    messages = ([{"role": "system", "content": sys}] if sys else []) + list(convo)
    # GPT-5.x는 max_tokens 대신 max_completion_tokens. reasoning 미사용이라 전액 응답에 쓰인다.
    resp = await _get_openai_client().chat.completions.create(
        model=model,
        messages=messages,
        max_completion_tokens=max_tokens or settings.llm_max_tokens,
        timeout=_timeout(timeout),
    )
    choices = getattr(resp, "choices", None) or []
    text = (choices[0].message.content or "") if choices else ""
    u = getattr(resp, "usage", None)
    if u is None:
        return LLMResult(text=text, input_tokens=0, output_tokens=0, model=model)
    prompt_tokens = getattr(u, "prompt_tokens", None) or 0
    details = getattr(u, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
    completion = getattr(u, "completion_tokens", None) or 0
    # 캐시 쓰기 토큰 **추정**(실 인보이스 대조 전까지 확정 아님). SDK 2.44.0의 PromptTokensDetails는
    # ['audio_tokens', 'cached_tokens']뿐이라 쓰기 토큰을 보고하지 않는다(확인함). 요금 모델상
    # prompt_tokens는 읽기/쓰기/일반입력 3버킷에 배타적으로 속하므로, 캐시가 걸리는 길이(1024+)면
    # 미적중분은 캐시에 기록된 것으로 보고 쓰기로, 그 미만이면 캐시 자체가 없어 전량 일반 입력으로 센다.
    # 오차: 정상(캐시 적중) 턴은 턴당 ~1%, 캐시 미스 턴은 최대 25% **과대** 계상(보수적 방향).
    # 배포 후 1주 실 인보이스와 대조해 5% 초과 괴리면 재조정. SDK가 쓰기 필드를 노출하면 실측으로 교체.
    uncached = max(0, prompt_tokens - cached)  # 캐시분 분리(이중계상 방지)
    cacheable = prompt_tokens >= _OPENAI_CACHE_MIN_PREFIX_TOKENS
    return LLMResult(
        text=text,
        input_tokens=0 if cacheable else uncached,
        output_tokens=completion,
        cache_read_tokens=cached,
        cache_write_tokens=uncached if cacheable else 0,
        model=model,
    )


# =====================================================================================
# W4 — 툴콜 계약(generate_step). 위의 generate() 경로는 **무변경**(워커·일기·복원이 계속 쓴다).
#
# generate()는 텍스트 in/텍스트 out이라 도구 호출을 표현할 수 없고, _generate_openai는
# `choices[0].message.content`를 문자열로 가정한다 — 툴콜 응답은 content가 None이라 빈 답변으로
# 오인된다. 아래 계약은 typed transcript로 그 구멍을 닫는다: content=None은 "텍스트 없음"이지
# "빈 답변"이 아니다(text가 None으로 그대로 나간다).
# =====================================================================================


@dataclass(frozen=True)
class UserText:
    text: str


@dataclass(frozen=True)
class AssistantText:
    text: str


@dataclass
class ToolCall:
    """모델이 요청한 도구 호출 1건.

    arguments는 validation_error가 None일 때만 검증 완료 값이다. 인자 JSON 파싱 실패·미지 도구명·
    스키마 불일치는 여기서 validation_error로 표시만 하고 **실행하지 않는다** — 호출측이
    unavailable_result()로 형식만 닫는다.
    """
    call_id: str
    tool_name: str
    arguments: dict
    validation_error: str | None = None  # malformed_json|unknown_tool|schema_mismatch|malformed_call


@dataclass(frozen=True)
class AssistantToolCalls:
    calls: tuple[ToolCall, ...]


@dataclass(frozen=True)
class ControlIntent:
    """모델이 표명한 기억 제어 의도. 호출측 Phase 2만 이를 durable 변경으로 적용한다.

    자유문 정규식으로 보충하지 않는다(control_tools로 넘긴 도구명만 인식). kind는 "pin"|"forget"뿐이고
    그 밖은 validation error로 버린다.
    """
    kind: str
    target_fact_ids: tuple[uuid.UUID, ...] = ()
    value: str | None = None


CONTROL_INTENT_KINDS = ("pin", "forget")


@dataclass(frozen=True)
class ToolResult:
    """도구 실행 결과 1건. 불변식: unavailable → data=None + 비어있지 않은 error_code / ok → error_code=None."""
    call_id: str
    tool_name: str
    status: Literal["ok", "unavailable"]
    data: dict | list | None
    schema_version: Literal[1] = 1
    error_code: str | None = None  # timeout|cancelled|invalid_arguments|tool_call_limit|dependency_error|internal
    truncated: bool = False
    duration_ms: int = 0  # trace 전용 — 모델 wire에 넣지 않는다


TranscriptItem = UserText | AssistantText | AssistantToolCalls | ToolResult


@dataclass
class StepResult:
    text: str | None  # None = 텍스트 블록 없음(툴콜만). 빈 답변("")과 구분해야 한다
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: LlmCall
    control_intents: list[ControlIntent] = field(default_factory=list)


class ToolContractError(ValueError):
    """typed transcript가 계약을 어겨 모델 호출을 만들 수 없음(호출 전에 터진다)."""


class UnsupportedProviderError(NotImplementedError):
    """툴콜 미지원 provider(Anthropic dormant)."""


def unavailable_result(
    call: ToolCall, *, error_code: str = "invalid_arguments", duration_ms: int = 0
) -> ToolResult:
    """실행하지 않은 호출을 형식만 닫는 ToolResult. 불변식(data=None + error_code)을 강제한다."""
    if not error_code:
        raise ToolContractError("unavailable 결과에는 error_code가 필요하다")
    return ToolResult(
        call_id=call.call_id,
        tool_name=call.tool_name,
        status="unavailable",
        data=None,
        error_code=error_code,
        duration_ms=duration_ms,
    )


def _check_result(r: ToolResult) -> None:
    if r.status == "unavailable":
        if r.data is not None:
            raise ToolContractError(f"unavailable 결과에 data가 있다 call_id={r.call_id}")
        if not r.error_code:
            raise ToolContractError(f"unavailable 결과에 error_code가 없다 call_id={r.call_id}")
    elif r.error_code is not None:
        raise ToolContractError(f"ok 결과에 error_code가 있다 call_id={r.call_id}")


def _dump_args(arguments: dict) -> str:
    """wire용 인자 직렬화 — 결정적(sort_keys)이라 같은 인자면 같은 문자열 = 프리픽스 캐시가 산다."""
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _dump_result(r: ToolResult) -> str:
    """모델이 읽는 도구 결과 JSON. duration_ms는 trace 전용이라 넣지 않는다."""
    return json.dumps(
        {
            "schema_version": r.schema_version,
            "tool": r.tool_name,
            "status": r.status,
            "data": r.data,
            "error_code": r.error_code,
            "truncated": r.truncated,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def to_openai_messages(
    system: str | list[str], transcript: Sequence[TranscriptItem]
) -> list[dict]:
    """typed transcript → OpenAI wire 메시지.

    형식 완결성을 여기서 강제한다: assistant tool call 메시지 뒤에는 **원래 순서대로** 모든 call_id의
    tool message가 정확히 하나씩 와야 하고, 하나라도 비면 다음 호출을 만들지 않고 예외를 던진다
    (미완결 transcript를 그대로 보내면 OpenAI 400이거나 더 나쁘게는 무응답 턴이 된다).
    """
    sys = system if isinstance(system, str) else "\n\n".join(b for b in system if b)
    messages: list[dict] = [{"role": "system", "content": sys}] if sys else []
    pending: list[ToolCall] = []  # 아직 결과가 안 붙은 호출(원래 순서)

    def _require_closed(item: object) -> None:
        if pending:
            missing = ", ".join(c.call_id for c in pending)
            raise ToolContractError(
                f"결과가 없는 tool call이 남아 있다: {missing} (다음 항목={type(item).__name__})"
            )

    for item in transcript:
        if isinstance(item, UserText):
            _require_closed(item)
            messages.append({"role": "user", "content": item.text})
        elif isinstance(item, AssistantText):
            _require_closed(item)
            messages.append({"role": "assistant", "content": item.text})
        elif isinstance(item, AssistantToolCalls):
            _require_closed(item)
            if not item.calls:
                raise ToolContractError("빈 AssistantToolCalls")
            ids = [c.call_id for c in item.calls]
            if len(set(ids)) != len(ids):
                raise ToolContractError(f"call_id 중복: {ids}")
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {
                                "name": c.tool_name,
                                "arguments": _dump_args(c.arguments),
                            },
                        }
                        for c in item.calls
                    ],
                }
            )
            pending = list(item.calls)
        elif isinstance(item, ToolResult):
            if not pending:
                raise ToolContractError(f"짝 없는 tool 결과 call_id={item.call_id}")
            expected = pending.pop(0)
            if expected.call_id != item.call_id:
                raise ToolContractError(
                    f"tool 결과 순서 불일치: {expected.call_id} 자리에 {item.call_id}"
                )
            if expected.tool_name != item.tool_name:
                raise ToolContractError(
                    f"tool 결과 도구명 불일치 call_id={item.call_id}: "
                    f"{expected.tool_name} != {item.tool_name}"
                )
            _check_result(item)
            messages.append(
                {"role": "tool", "tool_call_id": item.call_id, "content": _dump_result(item)}
            )
        else:  # pragma: no cover - 타입 계약 위반
            raise ToolContractError(f"알 수 없는 transcript 항목: {type(item).__name__}")
    _require_closed(None)
    return messages


def from_openai_messages(messages: Sequence[dict]) -> tuple[str, list[TranscriptItem]]:
    """wire → typed 역변환(같은 필드로 대칭). 왕복 대칭성 테스트와 replay 디버깅용."""
    system = ""
    items: list[TranscriptItem] = []
    by_id: dict[str, str] = {}  # call_id → tool_name(결과 복원에 필요)
    for m in messages:
        role = m.get("role")
        if role == "system":
            system = m.get("content") or ""
        elif role == "user":
            items.append(UserText(text=m.get("content") or ""))
        elif role == "assistant":
            raw = m.get("tool_calls") or []
            if not raw:
                items.append(AssistantText(text=m.get("content") or ""))
                continue
            calls = []
            for c in raw:
                fn = c["function"]
                calls.append(
                    ToolCall(
                        call_id=c["id"],
                        tool_name=fn["name"],
                        arguments=json.loads(fn["arguments"]),
                    )
                )
                by_id[c["id"]] = fn["name"]
            items.append(AssistantToolCalls(calls=tuple(calls)))
        elif role == "tool":
            payload = json.loads(m["content"])
            call_id = m["tool_call_id"]
            items.append(
                ToolResult(
                    call_id=call_id,
                    tool_name=payload.get("tool") or by_id.get(call_id, ""),
                    status=payload["status"],
                    data=payload.get("data"),
                    schema_version=payload.get("schema_version", 1),
                    error_code=payload.get("error_code"),
                    truncated=payload.get("truncated", False),
                )
            )
        else:  # pragma: no cover - wire 계약 위반
            raise ToolContractError(f"알 수 없는 wire role: {role!r}")
    return system, items


def _tool_names(tools: list[dict] | None) -> set[str]:
    """wire 도구 스키마에서 도구명 집합. 미지 도구명 판정에 쓴다."""
    names = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        name = (fn or {}).get("name") if isinstance(fn, dict) else t.get("name")
        if name:
            names.add(name)
    return names


def _parse_call(
    raw: object,
    index: int,
    *,
    known: set[str],
    input_models: Mapping[str, type[BaseModel]] | None,
) -> ToolCall:
    """wire tool_call 1건 → ToolCall. 검증 실패는 예외가 아니라 validation_error로 표시한다."""
    fn = getattr(raw, "function", None)
    call_id = getattr(raw, "id", None) or f"_missing_{index}"
    name = (getattr(fn, "name", None) or "") if fn is not None else ""
    raw_args = (getattr(fn, "arguments", None) or "") if fn is not None else ""
    if not getattr(raw, "id", None) or not name:
        return ToolCall(call_id, name, {}, validation_error="malformed_call")
    try:
        args = json.loads(raw_args) if raw_args else {}
    except (ValueError, TypeError):
        return ToolCall(call_id, name, {}, validation_error="malformed_json")
    if not isinstance(args, dict):
        return ToolCall(call_id, name, {}, validation_error="malformed_json")
    if known and name not in known:
        return ToolCall(call_id, name, {}, validation_error="unknown_tool")
    model_cls = (input_models or {}).get(name)
    if model_cls is not None:
        try:
            args = model_cls.model_validate(args).model_dump(mode="json")
        except ValidationError:
            return ToolCall(call_id, name, {}, validation_error="schema_mismatch")
    return ToolCall(call_id, name, args)


def _control_intent(call: ToolCall, kind: str) -> ControlIntent | None:
    """control 도구 호출 → ControlIntent. 부적합하면 None(계측만, 실행·저장 없음)."""
    if kind not in CONTROL_INTENT_KINDS or call.validation_error:
        return None
    raw_ids = call.arguments.get("target_fact_ids") or ()
    if isinstance(raw_ids, str) or not isinstance(raw_ids, (list, tuple)):
        return None
    try:
        ids = tuple(uuid.UUID(str(x)) for x in raw_ids)
    except (ValueError, AttributeError, TypeError):
        return None
    value = call.arguments.get("value")
    if value is not None and not isinstance(value, str):
        return None
    return ControlIntent(kind=kind, target_fact_ids=ids, value=value)


def _step_usage(u: object, model: str, purpose: str) -> LlmCall:
    """OpenAI usage → 회계 단위 LlmCall. 버킷 배분 규칙은 _generate_openai과 동일(등가 테스트로 고정)."""
    prompt_tokens = (getattr(u, "prompt_tokens", None) or 0) if u is not None else 0
    details = getattr(u, "prompt_tokens_details", None) if u is not None else None
    cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
    completion = (getattr(u, "completion_tokens", None) or 0) if u is not None else 0
    uncached = max(0, prompt_tokens - cached)
    cacheable = prompt_tokens >= _OPENAI_CACHE_MIN_PREFIX_TOKENS
    r = LLMResult(
        text="",
        input_tokens=0 if cacheable else uncached,
        output_tokens=completion,
        cache_read_tokens=cached,
        cache_write_tokens=uncached if cacheable else 0,
        model=model,
    )
    return LlmCall(
        provider="openai",
        model=model,
        purpose=purpose,
        input_tokens=r.input_tokens,
        output_tokens=r.output_tokens,
        cache_read_tokens=r.cache_read_tokens,
        cache_write_tokens=r.cache_write_tokens,
        billable=billable_tokens(r),
    )


async def generate_step(
    system: str | list[str],
    transcript: Sequence[TranscriptItem],
    *,
    tools: list[dict] | None,
    tool_choice: str,
    model: str,
    max_tokens: int,
    timeout: float,
    purpose: str = "tool_decide",
    input_models: Mapping[str, type[BaseModel]] | None = None,
    control_tools: Mapping[str, str] | None = None,
) -> StepResult:
    """도구 호출을 표현할 수 있는 1 step 호출. OpenAI 경로만(Anthropic은 dormant → 명시적 오류).

    - `content=None`은 빈 답변이 아니라 "텍스트 없음"이다(text=None으로 그대로 반환).
    - `tool_choice="none"`이면 도구를 강제로 못 쓰게 한다 — wire에 그대로 넘기고, 그래도 모델이
      호출을 뱉으면 버린다(최종 step이 다시 루프를 돌지 않게 하는 백스톱).
    - usage는 W1의 LlmCall로 반환해 TurnUsage에 그대로 누적한다.
    - control_intents는 control_tools로 넘긴 도구명만 인식하며 호출측이 별도 트랜잭션에서 적용한다.
      이 계층은 직접 쓰지 않고 자유문 정규식으로 보충하지 않는다.
    """
    if provider_for(model) != "openai":
        raise UnsupportedProviderError(
            f"generate_step은 OpenAI 경로만 지원한다(Anthropic 툴콜 미구현·dormant): model={model!r}"
        )
    messages = to_openai_messages(system, transcript)  # 미완결 transcript면 여기서 중단(호출 안 함)
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
        # GPT-5.6 Chat Completions는 함수 도구와 reasoning을 함께 받지 않는다. 이 경로는
        # 5초 안에 결정+도구+최종 응답을 끝내는 저지연 루프이기도 하므로 명시적으로 끈다.
        # 생략하면 모델 기본 reasoning이 적용돼 tools가 있는 요청이 API 400으로 실패한다.
        "reasoning_effort": "none",
        "timeout": _timeout(timeout),
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = tool_choice
    resp = await _get_openai_client().chat.completions.create(**kwargs)

    choices = getattr(resp, "choices", None) or []
    usage = _step_usage(getattr(resp, "usage", None), model, purpose)
    if not choices:  # content_filter 등 — 응답을 막지 않고 빈 step으로 닫는다
        return StepResult(text=None, tool_calls=[], finish_reason="", usage=usage, control_intents=[])
    choice = choices[0]
    message = getattr(choice, "message", None)
    text = getattr(message, "content", None) if message is not None else None
    raw_calls = (getattr(message, "tool_calls", None) or []) if message is not None else []

    # control 도구는 tools 스키마에 있든 없든 미지 도구가 아니다(실행은 안 하지만 의도는 읽는다).
    known = _tool_names(tools) | set(control_tools or {})
    calls: list[ToolCall] = []
    intents: list[ControlIntent] = []
    for i, raw in enumerate(raw_calls):
        call = _parse_call(raw, i, known=known, input_models=input_models)
        kind = (control_tools or {}).get(call.tool_name)
        if kind is not None:  # control 도구는 실행 대상이 아니다(shadow만)
            intent = _control_intent(call, kind)
            if intent is None:
                _log.info("control intent 폐기 tool=%s reason=validation", call.tool_name)
            else:
                intents.append(intent)
            continue
        calls.append(call)
    if tool_choice == "none" and calls:
        _log.warning("tool_choice=none인데 모델이 도구를 호출해 버린다 n=%d", len(calls))
        calls = []
    return StepResult(
        text=text,
        tool_calls=calls,
        finish_reason=getattr(choice, "finish_reason", "") or "",
        usage=usage,
        control_intents=intents,
    )

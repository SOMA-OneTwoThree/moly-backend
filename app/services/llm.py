"""LLM 래퍼 — 비스트리밍(완성본 반환) + 토큰 usage.

provider는 model-id 프리픽스로 라우팅한다: gpt-* → OpenAI, 그 외(claude-*) → Anthropic.
generate() 시그니처는 provider 무관하게 고정 — 호출부(chat·diary)는 provider를 몰라도 된다.
model 을 claude-* 로 되돌리면 _generate_anthropic 경로로 즉시 복귀(롤백·재사용).

대화는 HTTP 요청-응답 완성본(ARCHITECTURE). 스트리밍 없음.
토큰 집계 = 모델 실측 usage. Anthropic은 system prefix(페르소나+기억) 캐싱, OpenAI는 자동캐시.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

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
    cache_write_tokens: int = 0  # 캐시에 씀(5m 1.25× / 1h 2× 실원가). OpenAI는 항상 0(자동캐시)
    model: str = ""              # 실제 호출 모델 — _billable이 이 prefix로 provider별 가중치 선택


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


async def generate(
    system: str | list[str],
    convo: list[dict],
    *,
    max_tokens: int | None = None,
    model: str | None = None,
    cache_messages: bool = False,
    ttl_system: str = "5m",
    ttl_messages: str = "5m",
) -> LLMResult:
    """system(페르소나+기억) + convo(user/assistant) → 응답 텍스트 + 실측 토큰.

    model 미지정 = 대화 모델(settings.model_chat). 일기 self-check·기억통합은 utility 지정.
    provider는 model 프리픽스로 자동 분기. cache_messages/ttl_* 는 Anthropic 전용(OpenAI 자동캐시).
    """
    model = model or settings.model_chat
    if provider_for(model) == "openai":
        return await _generate_openai(system, convo, model=model, max_tokens=max_tokens)
    return await _generate_anthropic(
        system, convo, model=model, max_tokens=max_tokens,
        cache_messages=cache_messages, ttl_system=ttl_system, ttl_messages=ttl_messages,
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
) -> LLMResult:
    """Anthropic 경로(보존). system 리스트면 블록별 breakpoint. cache_messages=True면 마지막 메시지도 캐싱."""
    messages = _cache_last(convo, ttl_messages) if cache_messages else convo
    resp = await _get_client().messages.create(
        model=model,
        max_tokens=max_tokens or settings.llm_max_tokens,
        system=_system_blocks(system, ttl_system),
        messages=messages,
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
) -> LLMResult:
    """OpenAI 경로(신설). system(str|list) → messages[0] system 합침(평문, cache_control 미부착).

    OpenAI는 프리픽스 자동캐시라 cache_control/ttl 불필요. usage 정규화(이중계상 방지):
    input=prompt-cached / cache_read=cached / cache_write=0 / output=completion.
    방어: usage None·choices 빈·content None 에도 500 없이 빈 결과로 폴백(응답을 막지 않음).
    """
    sys = system if isinstance(system, str) else "\n\n".join(b for b in system if b)
    messages = ([{"role": "system", "content": sys}] if sys else []) + list(convo)
    # GPT-5.x는 max_tokens 대신 max_completion_tokens. reasoning 미사용이라 전액 응답에 쓰인다.
    resp = await _get_openai_client().chat.completions.create(
        model=model,
        messages=messages,
        max_completion_tokens=max_tokens or settings.llm_max_tokens,
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
    return LLMResult(
        text=text,
        input_tokens=max(0, prompt_tokens - cached),  # 캐시분 분리(이중계상 방지)
        output_tokens=completion,
        cache_read_tokens=cached,
        cache_write_tokens=0,
        model=model,
    )

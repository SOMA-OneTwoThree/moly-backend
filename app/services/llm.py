"""Anthropic Claude 래퍼 — 비스트리밍(완성본 반환) + 토큰 usage.

대화는 HTTP 요청-응답 완성본(ARCHITECTURE). 스트리밍 없음.
토큰 집계 = 모델 실측 usage(input+output). 안정된 system/대화 prefix 캐싱.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import settings

_client = None


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0   # 캐시에서 읽음(0.1× 실원가) — 기본 0이라 기존 positional 생성 호환
    cache_write_tokens: int = 0  # 캐시에 씀(5m 1.25× / 1h 2× 실원가)


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


def _cache_before_last(convo: list[dict], ttl: str) -> list[dict]:
    """마지막 user 메시지의 변동 블록 직전에 cache breakpoint를 둔다."""
    if len(convo) < 2:
        return convo
    out = [dict(m) for m in convo]
    previous = out[-2]
    content = previous["content"]
    if isinstance(content, str):
        previous["content"] = [
            {"type": "text", "text": content, "cache_control": _cc(ttl)}
        ]
    else:
        blocks = [dict(block) for block in content]
        blocks[-1] = {**blocks[-1], "cache_control": _cc(ttl)}
        previous["content"] = blocks
    return out


async def generate(
    system: str | list[str],
    convo: list[dict],
    *,
    max_tokens: int | None = None,
    model: str | None = None,
    cache_messages: bool = False,
    cache_before_last: bool = False,
    ttl_system: str = "5m",
    ttl_messages: str = "5m",
) -> LLMResult:
    """system + convo(user/assistant) → 응답 텍스트 + 실측 토큰.

    system이 리스트면 블록별 breakpoint. cache_messages=True면 대화도 캐싱한다.
    cache_before_last=True면 변동하는 마지막 user 메시지 직전까지만 캐싱한다.
    model 미지정 = 대화 모델(Sonnet). 일기 self-check는 utility(Haiku) 지정.
    """
    if cache_messages and cache_before_last:
        messages = _cache_before_last(convo, ttl_messages)
    else:
        messages = _cache_last(convo, ttl_messages) if cache_messages else convo
    resp = await _get_client().messages.create(
        model=model or settings.anthropic_model_chat,
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
    )

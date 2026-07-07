"""Anthropic Claude 래퍼 — 비스트리밍(완성본 반환) + 토큰 usage.

대화는 HTTP 요청-응답 완성본(ARCHITECTURE). 스트리밍 없음.
토큰 집계 = 모델 실측 usage(input+output). system prefix(페르소나+기억) 캐싱.
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


async def generate(
    system: str, convo: list[dict], *, max_tokens: int | None = None
) -> LLMResult:
    """system(페르소나+기억) + convo(user/assistant) → 응답 텍스트 + 실측 토큰."""
    resp = await _get_client().messages.create(
        model=settings.anthropic_model_chat,
        max_tokens=max_tokens or settings.llm_max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=convo,
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return LLMResult(
        text=text,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )

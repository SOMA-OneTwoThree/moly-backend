"""대화 모델 A/B 평가(dev 전용) — 동일 페르소나·대화를 여러 provider에 투입해 품질·속도·비용 측정.

프로덕션 chat 경로와 완전히 분리(운영 무영향). anthropic/openai/gemini 어댑터 + 단가표.
품질은 사람이 응답을 읽고 판단하고, 속도(지연ms)·토큰·추정비용은 여기서 실측한다.
어댑터 SDK import는 함수 안에서만(모듈 import 비용·키 부재 안전).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.config import settings

_log = logging.getLogger("moly-backend")

# 단가(USD per 1M 토큰) — 2026-07 기준. compare에서 실비용 추정용.
PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.0, 15.0),           # 현행 대화 모델(비교 기준)
    "claude-haiku-4-5-20251001": (1.0, 5.0),  # 참고
    "gpt-5.6-sol": (5.0, 30.0),
    "gpt-5.6-terra": (2.5, 15.0),
    "gpt-5.6-luna": (1.0, 6.0),
    "gemini-3.6-flash": (1.5, 7.5),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}

# compare 기본 모델셋(현행 Sonnet + 후보 4종)
DEFAULT_MODELS: list[dict[str, str]] = [
    {"provider": "anthropic", "model": "claude-sonnet-5"},
    {"provider": "openai", "model": "gpt-5.6-luna"},
    {"provider": "openai", "model": "gpt-5.6-terra"},
    {"provider": "gemini", "model": "gemini-3.6-flash"},
    {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
]


@dataclass
class EvalResult:
    provider: str
    model: str
    text: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    est_cost_usd: float
    error: str | None = None


def _cost(model: str, in_t: int, out_t: int) -> float:
    p = PRICING.get(model)
    if p is None:
        return 0.0  # 단가 미등록 모델은 0(추정 불가 표시)
    return round(in_t / 1e6 * p[0] + out_t / 1e6 * p[1], 6)


async def _anthropic(model, system, messages, max_tokens):  # noqa: ANN001
    from app.services import llm

    # llm.generate는 system을 프롬프트 캐시(cache_control)로 감싸 페르소나가 cache_write로 빠진다
    # → input_tokens가 실제보다 작게 잡혀 openai/gemini와 불공정. A/B는 캐시 없이 평문 호출로
    # 전체 입력 토큰을 세서 공정하게 비교한다(프로덕션 캐시 이점은 별개 사안).
    client = llm._get_client()
    resp = await client.messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=list(messages)
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    u = resp.usage
    return text, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0


async def _openai(model, system, messages, max_tokens):  # noqa: ANN001
    from openai import AsyncOpenAI

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY 미설정")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    # GPT-5.x는 max_tokens 대신 max_completion_tokens.
    resp = await client.chat.completions.create(
        model=model, messages=msgs, max_completion_tokens=max_tokens
    )
    text = resp.choices[0].message.content or ""
    u = resp.usage
    return text, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)


# Gemini 3.x flash는 기본 thinking ON이라 max_output_tokens를 사고에 다 써 응답이 잘린다.
# 프로덕션 chat은 짧은 저지연 응답이라 thinking을 최소화해야 다른 모델과 공정. budget=0은 이 모델에서
# 400(미지원)이라 낮은 양수(128)로 사실상 끈다(실측 thoughts=0). 비용은 thoughts 토큰까지 out에 합산.
_GEMINI_THINKING_BUDGET = 128


async def _gemini(model, system, messages, max_tokens):  # noqa: ANN001
    from google import genai
    from google.genai import types

    if not settings.gemini_api_key:
        raise RuntimeError("GEMINI_API_KEY 미설정")
    client = genai.Client(api_key=settings.gemini_api_key)
    # role 매핑: assistant → model. system은 별도 지시로.
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    cfg = types.GenerateContentConfig(
        system_instruction=system or None,
        max_output_tokens=max_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=_GEMINI_THINKING_BUDGET),
    )
    resp = await client.aio.models.generate_content(model=model, contents=contents, config=cfg)
    text = resp.text or ""
    um = resp.usage_metadata
    in_t = (getattr(um, "prompt_token_count", None) or 0) if um else 0
    # 출력 비용 = 표시 응답(candidates) + 사고(thoughts) 토큰(Gemini는 사고도 출력으로 과금).
    cand = (getattr(um, "candidates_token_count", None) or 0) if um else 0
    thoughts = (getattr(um, "thoughts_token_count", None) or 0) if um else 0
    return text, in_t, cand + thoughts


_ADAPTERS = {"anthropic": _anthropic, "openai": _openai, "gemini": _gemini}


async def run_eval(
    provider: str, model: str, system: str, messages: list[dict], *, max_tokens: int = 1024
) -> EvalResult:
    """한 모델 1회 호출 — 응답·지연·토큰·비용. 실패는 error에 담아 반환(compare에서 한 모델 실패가
    전체를 막지 않게). 절대 예외를 던지지 않는다."""
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        return EvalResult(provider, model, None, 0, 0, 0, 0.0, error=f"알 수 없는 provider: {provider}")
    start = time.perf_counter()
    try:
        text, in_t, out_t = await adapter(model, system, messages, max_tokens)
        ms = int((time.perf_counter() - start) * 1000)
        return EvalResult(provider, model, text, ms, in_t, out_t, _cost(model, in_t, out_t))
    except Exception as e:  # noqa: BLE001  # 한 모델 실패가 비교 전체를 막지 않게
        # dev 전용 툴이라 상세 에러는 개발자에게 유용 → error 필드에 유지하되 로그도 남긴다.
        _log.warning("model_eval 실패 provider=%s model=%s err=%r", provider, model, e)
        ms = int((time.perf_counter() - start) * 1000)
        return EvalResult(provider, model, None, ms, 0, 0, 0.0, error=f"{type(e).__name__}: {e}")

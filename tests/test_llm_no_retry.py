"""마감 있는 LLM 호출은 SDK 자동 재시도를 하지 않는다 — 실제 openai SDK + 가짜 HTTP 전송으로 고정.

2026-10-01 운영 5xx: 1홉 timeout 5.85초가 SDK 기본 재시도(max_retries=2) 때문에 18.9초가 됐고,
런타임의 fallback(남은 시간 ≥1.5초)이 한 번도 못 돌았다. 기존 테스트는 generate_step 자체를
대역으로 바꿔서 SDK 안의 재시도가 보이지 않았다 — 그래서 여기는 SDK를 그대로 쓰고 전송만 바꾼다.
"""
import httpx
import openai
import pytest

from app.services import llm as m
from app.services.llm import UserText

MODEL = "gpt-6-luna"


def _client(handler) -> openai.AsyncOpenAI:
    # max_retries는 운영 기본값(2) 그대로 둔다 — 재시도를 끄는 건 호출측 책임이다.
    return openai.AsyncOpenAI(
        api_key="test",
        base_url="https://api.openai.test/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _timeout_handler(attempts: list):
    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    return handler


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    # 재시도가 있는 경로도 백오프(0.5~1초)를 기다리지 않게 — 횟수만 본다.
    monkeypatch.setattr(
        openai._base_client.BaseClient, "_calculate_retry_timeout", lambda *a, **k: 0.0
    )


async def test_generate_step_makes_exactly_one_attempt_on_timeout(monkeypatch):
    attempts: list = []
    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(_timeout_handler(attempts)))
    with pytest.raises(openai.APITimeoutError):
        await m.generate_step(
            "p", [UserText("hi")], tools=None, tool_choice="auto",
            model=MODEL, max_tokens=64, timeout=5.85,
        )
    assert len(attempts) == 1  # 예전: 3회(= timeout × 3 + 백오프)


async def test_generate_without_retries_makes_one_attempt(monkeypatch):
    attempts: list = []
    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(_timeout_handler(attempts)))
    with pytest.raises(openai.APITimeoutError):
        await m.generate("p", [{"role": "user", "content": "hi"}], model=MODEL, sdk_retries=False)
    assert len(attempts) == 1


async def test_background_generate_keeps_sdk_retries(monkeypatch):
    """워커·일기는 마감이 없다 — SDK 재시도(기본 2회)를 그대로 쓴다."""
    attempts: list = []
    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(_timeout_handler(attempts)))
    with pytest.raises(openai.APITimeoutError):
        await m.generate("p", [{"role": "user", "content": "hi"}], model=MODEL)
    assert len(attempts) == 3


def _req() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.test/v1/chat/completions")


def _status(cls, code: int):
    return cls("x", response=httpx.Response(code, request=_req()), body=None)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError("agent turn deadline exceeded"), True),
        (openai.APITimeoutError(request=_req()), True),
        (openai.APIConnectionError(request=_req()), True),
        (_status(openai.RateLimitError, 429), True),
        (_status(openai.APIStatusError, 408), True),
        (_status(openai.ConflictError, 409), True),
        (_status(openai.InternalServerError, 500), True),
        (_status(openai.InternalServerError, 503), True),
        (_status(openai.BadRequestError, 400), False),
        (_status(openai.AuthenticationError, 401), False),
        (_status(openai.NotFoundError, 404), False),
        (RuntimeError("bug"), False),
    ],
)
def test_is_transient_failure(exc, expected):
    assert m.is_transient_failure(exc) is expected


# --- 같은 timeout 안의 재시도 1회(빨리 끝난 일시 오류만) ---------------------------------------
def _ok_body(text="응."):
    return {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    }


def _seq_handler(attempts: list, first: httpx.Response, *, seen: list | None = None):
    """첫 요청은 주어진 오류 응답, 이후는 정상 응답. seen에는 (시각, read timeout)을 남긴다."""
    import json as _json
    import time as _time

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(_json.loads(request.content))
        if seen is not None:
            seen.append((_time.monotonic(), request.extensions["timeout"]["read"]))
        return first if len(attempts) == 1 else httpx.Response(200, json=_ok_body())

    return handler


async def _decide(monkeypatch, handler, timeout: float):
    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(handler))
    return await m.generate_step(
        "p", [UserText("hi")], tools=None, tool_choice="auto",
        model=MODEL, max_tokens=64, timeout=timeout,
    )


async def test_fast_5xx_is_retried_once_within_budget_on_another_route(monkeypatch):
    """502가 바로 오면 SDK 재시도 대신 같은 예산 안에서 한 번 더 — 다른 서버로(prompt_cache_key)."""
    attempts: list = []
    step = await _decide(monkeypatch, _seq_handler(attempts, httpx.Response(502)), timeout=4.0)
    assert step.text == "응."
    assert len(attempts) == 2
    assert "prompt_cache_key" not in attempts[0] and attempts[1]["prompt_cache_key"].startswith("retry-")


async def test_retry_gets_only_the_rest_of_the_budget(monkeypatch):
    """재시도의 timeout = 원래 timeout − 이미 쓴 시간 − 대기. 이게 깨지면 SDK 재시도와 같은 사고가 난다."""
    attempts: list = []
    seen: list = []
    await _decide(monkeypatch, _seq_handler(attempts, httpx.Response(502), seen=seen), timeout=4.0)
    (t1, first_timeout), (t2, retry_timeout) = seen
    assert first_timeout == pytest.approx(4.0)
    assert retry_timeout <= 4.0 - m._IN_BUDGET_RETRY_BACKOFF_S + 1e-6
    assert (t2 - t1) + retry_timeout <= 4.0 + 0.05  # 두 시도 합계가 원래 예산 안


async def test_short_retry_after_429_is_honored_within_budget(monkeypatch):
    attempts: list = []
    seen: list = []
    first = httpx.Response(429, headers={"retry-after-ms": "300"})
    step = await _decide(monkeypatch, _seq_handler(attempts, first, seen=seen), timeout=4.0)
    assert step.text == "응." and len(attempts) == 2
    assert seen[1][0] - seen[0][0] >= 0.3  # Retry-After를 실제로 기다렸다


async def test_rejected_retry_keeps_the_original_transient_verdict(monkeypatch):
    """재시도 자체가 400으로 거부돼도 판정은 첫 실패(502) — 호출측이 fallback·503을 고를 수 있게."""
    import json as _json

    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_json.loads(request.content))
        return httpx.Response(502 if len(calls) == 1 else 400)

    with pytest.raises(openai.InternalServerError) as ei:
        await _decide(monkeypatch, handler, timeout=4.0)
    assert len(calls) == 2 and isinstance(ei.value.__cause__, openai.BadRequestError)


async def test_chat_generate_without_sdk_retries_still_retries_fast_failures(monkeypatch):
    """채팅 단발 경로(agent 비활성)도 같은 예산 안의 재시도를 탄다."""
    attempts: list = []
    monkeypatch.setattr(
        m, "_get_openai_client", lambda: _client(_seq_handler(attempts, httpx.Response(502)))
    )
    r = await m.generate(
        "p", [{"role": "user", "content": "hi"}], model=MODEL, timeout=4.0, sdk_retries=False
    )
    assert r.text == "응." and len(attempts) == 2
    assert attempts[1]["prompt_cache_key"].startswith("retry-")


async def test_long_retry_after_is_not_waited_out(monkeypatch):
    """Retry-After가 남은 예산보다 길면 기다리지 않는다 — SDK는 20초를 그대로 기다렸다(실측 23초 응답)."""
    attempts: list = []
    first = httpx.Response(429, headers={"retry-after": "20"})
    with pytest.raises(openai.RateLimitError):
        await _decide(monkeypatch, _seq_handler(attempts, first), timeout=4.0)
    assert len(attempts) == 1


async def test_no_retry_when_budget_is_too_small(monkeypatch):
    attempts: list = []
    with pytest.raises(openai.InternalServerError):
        await _decide(monkeypatch, _seq_handler(attempts, httpx.Response(502)), timeout=1.0)
    assert len(attempts) == 1


async def test_request_errors_are_not_retried(monkeypatch):
    attempts: list = []
    with pytest.raises(openai.BadRequestError):
        await _decide(monkeypatch, _seq_handler(attempts, httpx.Response(400)), timeout=4.0)
    assert len(attempts) == 1


# --- run_turn ↔ 실제 SDK 경계(이번 사고가 숨어 있던 곳) --------------------------------------
async def test_run_turn_hop1_timeout_reaches_toolless_fallback_through_real_sdk(monkeypatch):
    """도구 홉이 ReadTimeout이면 SDK가 같은 요청을 다시 보내지 않고, 런타임 fallback(도구 없음)이 답한다."""
    import json as _json
    import time
    import uuid
    from datetime import date

    from app.services import usage_ledger
    from app.services.agent import runtime
    from app.services.agent.config import build_snapshot
    from app.services.agent.tools.registry import REGISTRY

    async def _noop(*a, **k):
        return None

    for name in ("open_call", "close_call", "close_failed"):
        monkeypatch.setattr(usage_ledger, name, _noop)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        seen.append("tools" if body.get("tools") else "no-tools")
        if body.get("tools"):
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json=_ok_body("응, 들었어."))

    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(handler))
    cfg = build_snapshot({"agent_enabled": True, "agent_canary_pct": 100.0})
    turn = await runtime.run_turn(
        ["페르소나"], [{"role": "user", "content": "오늘 피곤했어"}],
        config=cfg, user_id=uuid.uuid4(), language="ko", activity_date=date(2026, 10, 1),
        user_text="오늘 피곤했어", registry=REGISTRY, deadline=time.monotonic() + 8.35,
    )
    assert turn.text == "응, 들었어." and turn.skipped == "deadline"
    assert seen == ["tools", "no-tools"]  # 예전: tools ×3(SDK 재시도) 뒤 fallback 없이 500

"""검증 A 보강 — 원장 기록 지연이 턴 마감을 밀지 않고, 거부된 재시도가 로그에 남는다."""
import asyncio
import logging
import uuid
from datetime import date

import httpx
import openai
import pytest

from app.services import llm as m
from app.services import usage_ledger
from app.services.llm import UserText
from tests.test_llm_no_retry import _client, _ok_body

MODEL = "gpt-6-luna"


def _ctx() -> usage_ledger.LedgerContext:
    return usage_ledger.LedgerContext(
        lane=usage_ledger.LANE_FOREGROUND, purpose="chat", user_id=uuid.uuid4(),
        activity_date=date(2026, 10, 1),
    )


async def test_generate_step_timeout_shrinks_by_ledger_open_latency(monkeypatch):
    """open_call(DB)이 0.3초 걸리면 실제 HTTP timeout은 그만큼 짧아져 합계가 호출측 timeout 안에 남는다."""
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_ok_body())

    async def slow_open(ctx, **kw):
        await asyncio.sleep(0.3)
        return None

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(handler))
    monkeypatch.setattr(usage_ledger, "open_call", slow_open)
    monkeypatch.setattr(usage_ledger, "close_call", _noop)
    await m.generate_step(
        "p", [UserText("hi")], tools=None, tool_choice="auto",
        model=MODEL, max_tokens=64, timeout=2.0, ledger=_ctx(),
    )
    assert seen and seen[0] <= 2.0 - 0.3 + 0.02
    assert seen[0] > 1.0  # 과하게 깎지도 않는다


async def test_generate_step_timeout_unchanged_without_ledger(monkeypatch):
    seen: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=_ok_body())

    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(handler))
    await m.generate_step(
        "p", [UserText("hi")], tools=None, tool_choice="auto", model=MODEL, max_tokens=64, timeout=2.0,
    )
    assert seen[0] == 2.0


async def test_rejected_in_budget_retry_is_logged(monkeypatch, caplog):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(502 if len(calls) == 1 else 400)

    monkeypatch.setattr(m, "_get_openai_client", lambda: _client(handler))
    with caplog.at_level(logging.WARNING):
        with pytest.raises(openai.InternalServerError):
            await m.generate_step(
                "p", [UserText("hi")], tools=None, tool_choice="auto",
                model=MODEL, max_tokens=64, timeout=4.0,
            )
    rec = [r for r in caplog.records if "llm_retry_rejected" in r.getMessage()]
    assert rec and '"status": 400' in rec[0].getMessage()

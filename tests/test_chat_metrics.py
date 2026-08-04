"""W2 계측 — post_message 턴별 구조화 로그(chat_turn_metrics). 응답 경로를 막지 않는지가 핵심."""
import json
import logging
from types import SimpleNamespace

from app.services import chat as chat_service
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services.llm import LLMResult
from tests.test_chat import FakeSession, _gating

UID = "11111111-1111-1111-1111-111111111111"

_REQUIRED_FIELDS = (
    "replay", "total_ms", "phase1_ms", "llm_ms", "repair_ms",
    "egress_ms", "phase2_ms", "prompt_tokens", "cache_read_tokens", "cache_write_tokens",
    "cache_read_ratio", "billable", "lang", "used_tools", "context_ms",
)


def _turn_metrics_payload(caplog) -> dict:
    for rec in caplog.records:
        if rec.getMessage().startswith("chat_turn_metrics "):
            return json.loads(rec.getMessage().removeprefix("chat_turn_metrics "))
    raise AssertionError("chat_turn_metrics 로그가 배출되지 않음")


async def test_turn_metrics_logs_all_required_fields(monkeypatch, caplog):
    async def _res(session, user_id):
        return _gating()

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="그냥 그랬어.", input_tokens=10, output_tokens=20, cache_read_tokens=30)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)

    caplog.set_level(logging.INFO, logger="moly-backend")
    req = SimpleNamespace(text="오늘 어땠어?", greeting_id=None)
    out = await chat_service.post_message(FakeSession(), UID, req, "idem-metrics-1")

    assert out.reply.content == "그냥 그랬어."
    payload = _turn_metrics_payload(caplog)
    for field in _REQUIRED_FIELDS:
        assert field in payload, f"{field} 누락"

    assert payload["replay"] is False
    assert payload["lang"] == "ko"
    assert payload["used_tools"] is False  # 도구 루프 미구현(W5) — 항상 False가 정상
    assert payload["billable"] > 0
    assert payload["prompt_tokens"] == 10 + 30 + 0  # input + cache_read + cache_write
    assert payload["cache_read_tokens"] == 30
    assert payload["cache_read_ratio"] == 30 / 40
    # monotonic 구간 — 전부 음수 아님, total이 각 구간 합보다 작지 않음
    for k in ("phase1_ms", "llm_ms", "repair_ms", "egress_ms", "phase2_ms"):
        assert payload[k] is not None and payload[k] >= 0
    assert payload["total_ms"] >= payload["phase1_ms"]


async def test_turn_metrics_cache_read_ratio_null_when_no_prompt_tokens(monkeypatch, caplog):
    # 분모(prompt_tokens) 0이면 cache_read_ratio는 null이어야 한다(0으로 나누기 회피).
    async def _res(session, user_id):
        return _gating()

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="응.", input_tokens=0, output_tokens=5, cache_read_tokens=0, cache_write_tokens=0)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)

    caplog.set_level(logging.INFO, logger="moly-backend")
    req = SimpleNamespace(text="hi", greeting_id=None)
    await chat_service.post_message(FakeSession(), UID, req, "idem-metrics-2")

    payload = _turn_metrics_payload(caplog)
    assert payload["prompt_tokens"] == 0
    assert payload["cache_read_ratio"] is None


async def test_turn_metrics_replay_flagged_on_idempotent_cache_hit(monkeypatch, caplog):
    cached_response = {
        "greeting": None,
        "user_message": {"message_id": "1", "created_at": "2026-07-07T00:00:00+00:00"},
        "reply": {"message_id": "2", "content": "이전 응답", "created_at": "2026-07-07T00:00:00+00:00"},
        "tokens_used": 100,
        "tokens_remaining": 19_900,
        "review_prompt": False,
    }
    cached = SimpleNamespace(response=cached_response)
    session = FakeSession(get_map={"IdempotencyKey": cached})

    caplog.set_level(logging.INFO, logger="moly-backend")
    req = SimpleNamespace(text="재시도", greeting_id=None)
    await chat_service.post_message(session, UID, req, "same-key")

    payload = _turn_metrics_payload(caplog)
    assert payload["replay"] is True
    assert payload["llm_ms"] is None  # 순수 replay라 LLM 구간 자체가 없음


async def test_post_message_survives_metrics_log_failure(monkeypatch, caplog):
    """로그 배출 중 예외가 나도 post_message 응답이 막히면 안 된다(계측 실패 ≠ 응답 실패)."""
    async def _res(session, user_id):
        return _gating()

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="응 그래.", input_tokens=10, output_tokens=20)

    def _boom_info(*a, **k):
        raise RuntimeError("로그 핸들러 고장")

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    monkeypatch.setattr(chat_service._log, "info", _boom_info)

    caplog.set_level(logging.WARNING, logger="moly-backend")
    req = SimpleNamespace(text="안녕", greeting_id=None)
    out = await chat_service.post_message(FakeSession(), UID, req, "idem-metrics-boom")

    assert out.reply.content == "응 그래."  # 계측 로그가 죽어도 정상 응답은 살아있어야 함

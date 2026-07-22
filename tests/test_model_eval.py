"""대화 모델 A/B 평가 서비스 — 비용 계산·디스패치·실패격리(네트워크 없이 어댑터 mock)."""
import pytest

from app.services import model_eval


def test_cost_known_model():
    # gpt-5.6-luna = ($1 in / $6 out) per 1M. 1000 in + 500 out.
    assert model_eval._cost("gpt-5.6-luna", 1000, 500) == pytest.approx(0.001 + 0.003)
    # gemini-3.5-flash-lite = ($0.30 / $2.50)
    assert model_eval._cost("gemini-3.5-flash-lite", 2000, 1000) == pytest.approx(0.0006 + 0.0025)


def test_cost_unknown_model_is_zero():
    assert model_eval._cost("mystery-model", 1000, 1000) == 0.0


async def test_run_eval_unknown_provider_returns_error_not_raise():
    r = await model_eval.run_eval("bogus", "x", "sys", [{"role": "user", "content": "안녕"}])
    assert r.error is not None and "provider" in r.error
    assert r.text is None and r.est_cost_usd == 0.0


async def test_run_eval_dispatches_and_computes_cost(monkeypatch):
    async def _fake(model, system, messages, max_tokens):
        assert system == "sys" and messages[0]["content"] == "안녕"
        return "응답이야", 1000, 500

    monkeypatch.setitem(model_eval._ADAPTERS, "openai", _fake)
    r = await model_eval.run_eval(
        "openai", "gpt-5.6-luna", "sys", [{"role": "user", "content": "안녕"}]
    )
    assert r.error is None
    assert r.text == "응답이야"
    assert r.input_tokens == 1000 and r.output_tokens == 500
    assert r.est_cost_usd == pytest.approx(0.004)
    assert r.latency_ms >= 0


async def test_run_eval_isolates_adapter_failure(monkeypatch):
    async def _boom(model, system, messages, max_tokens):
        raise RuntimeError("키 없음")

    monkeypatch.setitem(model_eval._ADAPTERS, "gemini", _boom)
    r = await model_eval.run_eval("gemini", "gemini-3.6-flash", "", [{"role": "user", "content": "hi"}])
    assert r.text is None
    assert r.error is not None and "키 없음" in r.error  # 예외를 던지지 않고 담아 반환


def test_default_models_and_pricing_aligned():
    # 기본 비교 셋의 모든 모델은 단가표에 있어야 비용 추정이 유의미.
    for d in model_eval.DEFAULT_MODELS:
        assert d["model"] in model_eval.PRICING, d["model"]
        assert d["provider"] in ("anthropic", "openai", "gemini")

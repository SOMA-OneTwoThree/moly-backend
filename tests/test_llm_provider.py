"""llm provider dispatch(OpenAI/Anthropic) + usage 정규화 + billable 가중치 회귀.

OpenAI 전환 시 회계 이중계상·크래시·롤백 보존을 지킨다.
"""
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import chat as c
from app.services import llm as llm_module
from app.services.llm import LLMResult, provider_for


# --- provider 판정 ---
def test_provider_for_prefix():
    assert provider_for("gpt-5.6-terra") == "openai"
    assert provider_for("gpt-5.6-luna") == "openai"
    assert provider_for("claude-sonnet-5") == "anthropic"
    assert provider_for("claude-haiku-4-5-20251001") == "anthropic"
    assert provider_for("") == "anthropic"   # 기본 = Anthropic(dormant 롤백 안전)
    assert provider_for(None) == "anthropic"


# --- OpenAI 어댑터 mock ---
class _FakeCompletions:
    def __init__(self, resp):
        self._resp = resp

    async def create(self, **kw):
        self._resp._kw = kw  # 호출 인자 캡처(검증용)
        return self._resp


class _FakeClient:
    def __init__(self, resp):
        self.chat = SimpleNamespace(completions=_FakeCompletions(resp))


def _usage(prompt, completion, cached):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )


def _resp(text, usage):
    choices = [SimpleNamespace(message=SimpleNamespace(content=text))] if text is not None else []
    return SimpleNamespace(choices=choices, usage=usage)


async def test_openai_usage_normalization(monkeypatch):
    resp = _resp("안녕", _usage(1000, 50, 800))
    monkeypatch.setattr(llm_module, "_get_openai_client", lambda: _FakeClient(resp))
    r = await llm_module.generate(
        "페르소나", [{"role": "user", "content": "hi"}], model="gpt-5.6-terra"
    )
    assert r.text == "안녕"
    assert r.input_tokens == 200      # 1000 - 800(cached), 이중계상 방지
    assert r.output_tokens == 50
    assert r.cache_read_tokens == 800
    assert r.cache_write_tokens == 0  # OpenAI 자동캐시는 write 과금 없음
    assert r.model == "gpt-5.6-terra"
    kw = resp._kw
    assert kw["messages"][0] == {"role": "system", "content": "페르소나"}  # system 합침·순서 보존
    assert kw["max_completion_tokens"] == 1024  # max_tokens 아님(GPT-5.x)


async def test_openai_system_list_joined_and_empty_dropped(monkeypatch):
    resp = _resp("응", _usage(10, 5, 0))
    monkeypatch.setattr(llm_module, "_get_openai_client", lambda: _FakeClient(resp))
    await llm_module.generate(
        ["페르소나", "", "기억"], [{"role": "user", "content": "hi"}], model="gpt-5.6-luna"
    )
    assert resp._kw["messages"][0]["content"] == "페르소나\n\n기억"  # 빈 블록 제거


async def test_openai_usage_none_is_safe(monkeypatch):
    resp = _resp("빈응답", None)  # usage 없음(게이트웨이/장애)
    monkeypatch.setattr(llm_module, "_get_openai_client", lambda: _FakeClient(resp))
    r = await llm_module.generate("p", [{"role": "user", "content": "hi"}], model="gpt-5.6-terra")
    assert r.text == "빈응답" and r.input_tokens == 0 and r.output_tokens == 0


async def test_openai_empty_choices_no_crash(monkeypatch):
    resp = SimpleNamespace(choices=[], usage=_usage(5, 0, 0))  # content_filter 등
    monkeypatch.setattr(llm_module, "_get_openai_client", lambda: _FakeClient(resp))
    r = await llm_module.generate("p", [{"role": "user", "content": "hi"}], model="gpt-5.6-terra")
    assert r.text == ""


async def test_openai_none_content_no_crash(monkeypatch):
    resp = _resp(None, _usage(5, 0, 0))
    resp.choices = [SimpleNamespace(message=SimpleNamespace(content=None))]
    monkeypatch.setattr(llm_module, "_get_openai_client", lambda: _FakeClient(resp))
    r = await llm_module.generate("p", [{"role": "user", "content": "hi"}], model="gpt-5.6-terra")
    assert r.text == ""


async def test_generate_routes_by_model_prefix(monkeypatch):
    seen = {}

    async def fake_anthropic(system, convo, *, model, **k):
        seen["anthropic"] = model
        return LLMResult("a", 1, 1, model=model)

    async def fake_openai(system, convo, *, model, **k):
        seen["openai"] = model
        return LLMResult("o", 1, 1, model=model)

    monkeypatch.setattr(llm_module, "_generate_anthropic", fake_anthropic)
    monkeypatch.setattr(llm_module, "_generate_openai", fake_openai)
    await llm_module.generate("p", [], model="claude-sonnet-5")
    await llm_module.generate("p", [], model="gpt-5.6-terra")
    assert seen == {"anthropic": "claude-sonnet-5", "openai": "gpt-5.6-terra"}


# --- billable: provider별 가중치 ---
def test_billable_openai_weights():
    # OpenAI: input + 6.0*out + 0.5*read + 0*write = 200 + 300 + 400 + 0 = 900
    r = LLMResult(
        "t", input_tokens=200, output_tokens=50,
        cache_read_tokens=800, cache_write_tokens=0, model="gpt-5.6-terra",
    )
    assert c._billable(r) == 900


def test_billable_anthropic_weights_preserved():
    # model 미지정("") → Anthropic 경로 = 기존 가중치 그대로(롤백 보존)
    warm = LLMResult("t", input_tokens=25, output_tokens=90, cache_read_tokens=3000, cache_write_tokens=0)
    assert c._billable(warm) == 25 + 5 * 90 + round(0.1 * 3000)
    cold = LLMResult("t", input_tokens=25, output_tokens=90, cache_read_tokens=0, cache_write_tokens=3000)
    assert c._billable(cold) == 25 + 5 * 90 + round(1.25 * 3000)


# --- 프로덕션 키 가드: 활성 모델(chat·diary·utility) 전부 검사(부분 롤백 안전) ---
def _prod(**over):
    base = dict(
        environment="production", revenuecat_webhook_auth="rc",
        model_chat="gpt-5.6-terra", model_diary="gpt-5.6-terra", model_utility="gpt-5.6-luna",
        openai_api_key="", anthropic_api_key="",
    )
    base.update(over)
    return Settings(**base)


def test_prod_guard_requires_openai_when_any_model_gpt():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        _prod(openai_api_key="").require_production_ready()
    _prod(openai_api_key="sk-x").require_production_ready()  # 키 있으면 통과


def test_prod_guard_partial_rollback_still_requires_openai():
    # chat만 claude로 롤백 + diary/utility는 gpt 유지 → openai 키 여전히 필수(일기 배치 보호)
    s = _prod(model_chat="claude-sonnet-5", anthropic_api_key="sk-a", openai_api_key="")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        s.require_production_ready()


def test_prod_guard_requires_anthropic_when_any_model_claude():
    s = _prod(
        model_chat="claude-sonnet-5", model_diary="claude-sonnet-5",
        model_utility="claude-haiku-4-5-20251001", openai_api_key="", anthropic_api_key="",
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        s.require_production_ready()

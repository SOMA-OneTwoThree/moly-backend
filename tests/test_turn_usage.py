"""턴 회계(W1) — 턴 내 모든 LLM 호출 합산. 예전엔 한자 복원 호출이 청구에서 통째로 샜다.

불변식: 저장된 billable_tokens = 그 턴이 실제로 부른 모든 호출의 _billable 합.
"""
from datetime import date
from types import SimpleNamespace

from app.models.message import Message
from app.services import chat as c
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services import memory as memory_module
from app.services.gating import Gating
from app.services.llm import LlmCall, LLMResult
from tests.test_chat import FakeSession

UID = "11111111-1111-1111-1111-111111111111"
MODEL = "gpt-5.6-luna"


def _call(purpose="chat", billable=100, **tok):
    base = dict(input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
    base.update(tok)
    return LlmCall(provider="openai", model=MODEL, purpose=purpose, billable=billable, **base)


# --- TurnUsage 합산 ---
def test_turn_usage_sums_billable_and_token_buckets():
    usage = c.TurnUsage([
        _call("chat", 130, input_tokens=10, output_tokens=20, cache_read_tokens=3000),
        _call("foreign_repair", 48, input_tokens=12, output_tokens=6),
    ])
    assert usage.total_billable == 178
    assert usage.totals == {
        "input_tokens": 22, "output_tokens": 26,
        "cache_read_tokens": 3000, "cache_write_tokens": 0,
    }


def test_turn_usage_empty_is_zero():
    assert c.TurnUsage().total_billable == 0
    assert set(c.TurnUsage().totals.values()) == {0}


def test_llm_call_carries_purpose_and_per_call_billable():
    r = LLMResult("t", input_tokens=10, output_tokens=20, model=MODEL)
    call = c._llm_call(r, "foreign_repair")
    assert call.purpose == "foreign_repair" and call.provider == "openai" and call.model == MODEL
    assert call.billable == c._billable(r) == 10 + 6 * 20


def test_turn_usage_mixed_providers_use_own_weights():
    """주 호출(OpenAI)·복원(Anthropic 롤백 중)이 섞여도 호출별 가중치로 계산된다."""
    o = c._llm_call(LLMResult("t", 0, 100, model="gpt-5.6-luna"), "chat")          # out 6.0
    a = c._llm_call(LLMResult("t", 0, 100, model="claude-sonnet-5"), "foreign_repair")  # out 5.0
    assert c.TurnUsage([o, a]).total_billable == 600 + 500


# --- post_message 전체 턴 ---
def _gating(**over):
    base = dict(
        profile=SimpleNamespace(language="ko", nickname="지훈", review_prompted_at=None, id=UID),
        activity_date=date(2026, 7, 7),
        entitlement={
            "plan": "trial", "tokens_remaining": 5000, "daily_token_limit": 100_000,
            "personal_diary_token_threshold": 2000,
        },
        tokens_used=1000,
        warning_threshold=3000,
        review_min_tokens=50_000,
    )
    base.update(over)
    return Gating(**base)


def _patch_repair_turn(monkeypatch):
    """주 chat 응답에 한자가 섞여 복원 호출이 1회 발동하는 턴. (chat billable, repair billable)."""
    async def _res(session, user_id):
        return _gating()

    async def _mem(user_id):
        return ""

    async def _gen(system, convo, **kw):
        if kw.get("model") == c.settings.model_utility:      # 복원 호출
            return LLMResult("나도 내 생각엔.", 12, 6, model=MODEL)
        return LLMResult("나도 我 생각엔.", 10, 20, model=MODEL)  # 주 chat 호출(한자 포함)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(memory_module, "load_for_context", _mem)
    monkeypatch.setattr(llm_module, "generate", _gen)
    return 10 + 6 * 20, 12 + 6 * 6  # 130, 48


async def test_repair_turn_bills_chat_plus_repair(monkeypatch):
    chat_bill, repair_bill = _patch_repair_turn(monkeypatch)
    session = FakeSession()
    req = SimpleNamespace(text="안녕", greeting_id=None)
    out = await c.post_message(session, UID, req, "idem-repair")

    assert out.reply.content == "나도 내 생각엔."           # 복원문이 응답
    reply_msg = next(m for m in session.added
                     if isinstance(m, Message) and m.sender == "moly")
    assert reply_msg.billable_tokens == chat_bill + repair_bill      # 178, 복원분 포함
    assert out.tokens_used == 1000 + chat_bill + repair_bill
    # 합계 컬럼도 턴 합(스키마 변경 없음)
    assert reply_msg.input_tokens == 22 and reply_msg.output_tokens == 26


async def test_no_repair_turn_bills_chat_only(monkeypatch):
    """복원 미발동 턴은 주 호출분만 — 합산 도입이 정상 턴을 부풀리지 않는다(회귀)."""
    async def _res(session, user_id):
        return _gating()

    async def _mem(user_id):
        return ""

    async def _gen(system, convo, **kw):
        return LLMResult("그냥 그랬어.", 10, 20, model=MODEL)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(memory_module, "load_for_context", _mem)
    monkeypatch.setattr(llm_module, "generate", _gen)
    session = FakeSession()
    req = SimpleNamespace(text="안녕", greeting_id=None)
    out = await c.post_message(session, UID, req, "idem-norepair")
    assert out.tokens_used == 1000 + 130


async def test_kill_switch_off_bills_chat_call_only(monkeypatch):
    """turn_usage_v2_enabled=False = 롤백 경로 — 복원 호출은 계측만, 차감·저장은 주 호출분만."""
    chat_bill, _repair_bill = _patch_repair_turn(monkeypatch)
    monkeypatch.setattr(c.settings, "turn_usage_v2_enabled", False)
    session = FakeSession()
    req = SimpleNamespace(text="안녕", greeting_id=None)
    out = await c.post_message(session, UID, req, "idem-ks")

    assert out.reply.content == "나도 내 생각엔."   # 복원 자체는 그대로 동작(회계만 롤백)
    reply_msg = next(m for m in session.added
                     if isinstance(m, Message) and m.sender == "moly")
    assert reply_msg.billable_tokens == chat_bill   # 130 — 복원분 미포함(기존 동작)
    assert reply_msg.input_tokens == 10 and reply_msg.output_tokens == 20
    assert out.tokens_used == 1000 + chat_bill

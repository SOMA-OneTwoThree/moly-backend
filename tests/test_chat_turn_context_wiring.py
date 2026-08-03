"""W3 배선 — post_message이 turn_context(build_context/render)를 어떻게 호출·삽입하는지.

turn_context.py 자체의 로직(버킷 경계·렌더 포맷·DB조회)은 test_turn_context.py가 담당한다.
여기는 chat.py 배선만 검증한다 — 그래서 turn_context.build_context/render를 monkeypatch로
갈아끼우고 실제 DB 조회는 흉내내지 않는다(FakeSession은 test_chat.py 관례 재사용).
"""
import logging
from types import SimpleNamespace

from app.services import chat as chat_service
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services import memory as memory_module
from app.services import turn_context as turn_context_module
from app.services.llm import LLMResult
from tests.test_chat import FakeSession, _gating
from tests.test_chat_metrics import _turn_metrics_payload

UID = "11111111-1111-1111-1111-111111111111"


async def _post(session, monkeypatch, *, reply="응.", capture: dict | None = None):
    async def _res(s, user_id):
        return _gating()

    async def _fake_mem(user_id):
        return ""

    async def _fake_llm(system, convo, **kw):
        if capture is not None:
            capture["system"] = system
            capture["convo"] = convo
        return LLMResult(text=reply, input_tokens=10, output_tokens=20)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(memory_module, "load_for_context", _fake_mem)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    req = SimpleNamespace(text="오늘 어땠어?", greeting_id=None)
    return await chat_service.post_message(session, UID, req, "idem-tc")


# 1) 킬스위치 off(기본값)면 기존과 완전히 동일 — build_context 미호출, 블록 미삽입.
async def test_disabled_by_default_skips_build_context_and_block(monkeypatch):
    assert chat_service.settings.current_turn_context_enabled is False  # 기본값 회귀 감시

    calls = []

    async def _fake_build_context(*a, **kw):
        calls.append((a, kw))
        return turn_context_module.CurrentTurnContext()

    monkeypatch.setattr(turn_context_module, "build_context", _fake_build_context)
    capture: dict = {}
    out = await _post(FakeSession(), monkeypatch, capture=capture)

    assert out.reply.content == "응."
    assert calls == []  # build_context 자체가 호출 안 됨
    last_content = capture["convo"][-1]["content"]
    assert last_content.endswith("오늘 어땠어?")  # 날짜 표식 외엔 블록 미삽입, 원문 그대로
    assert "[지금]" not in last_content


# 2) 활성화 시 상주 블록이 대화 배열의 마지막 user 메시지 안에 들어가고, system(_build_system)엔 없다.
async def test_enabled_appends_block_into_last_user_message_not_system(monkeypatch):
    monkeypatch.setattr(chat_service.settings, "current_turn_context_enabled", True)

    async def _fake_build_context(session, profile, *, is_first_today, now_utc):
        return turn_context_module.CurrentTurnContext(time_bucket="night")

    monkeypatch.setattr(turn_context_module, "build_context", _fake_build_context)
    capture: dict = {}
    await _post(FakeSession(), monkeypatch, capture=capture)

    last = capture["convo"][-1]
    assert last["role"] == "user"
    assert "오늘 어땠어?" in last["content"]
    assert "[지금] 밤" in last["content"]

    system_parts = capture["system"]
    system_text = "\n".join(system_parts) if isinstance(system_parts, list) else system_parts
    assert "[지금] 밤" not in system_text  # 페르소나 캐시 프리픽스 보호 — 절대 system엔 안 들어감


# 3) 별도 배열 항목으로 추가되지 않는다 — 블록이 붙어도 이번 턴 항목 수가 늘지 않고,
#    마지막 두 항목이 둘 다 user가 되는 일이 없어야 한다(Anthropic 첫 메시지 user 계약과 충돌 방지).
async def test_enabled_does_not_add_separate_array_item(monkeypatch):
    monkeypatch.setattr(chat_service.settings, "current_turn_context_enabled", True)

    async def _fake_build_context(session, profile, *, is_first_today, now_utc):
        return turn_context_module.CurrentTurnContext(time_bucket="day")

    monkeypatch.setattr(turn_context_module, "build_context", _fake_build_context)
    capture: dict = {}
    # 이전 대화 없음(기본 FakeSession) — 이번 턴 user 항목 하나만 생겨야 정상이다.
    # 별도 role로 잘못 추가됐다면 여기서 길이가 2(user, user)가 되어 실패한다.
    await _post(FakeSession(), monkeypatch, capture=capture)

    convo = capture["convo"]
    assert len(convo) == 1
    assert convo[0]["role"] == "user"
    assert "[지금] 낮" in convo[0]["content"]
    assert "오늘 어땠어?" in convo[0]["content"]


# 4) build_context가 예외를 던져도 대화는 정상 응답한다(fail-open) + 경고 로그.
async def test_build_context_exception_does_not_break_reply(monkeypatch, caplog):
    monkeypatch.setattr(chat_service.settings, "current_turn_context_enabled", True)

    async def _boom(*a, **kw):
        raise RuntimeError("build_context 고장")

    monkeypatch.setattr(turn_context_module, "build_context", _boom)
    caplog.set_level(logging.WARNING, logger="moly-backend")
    capture: dict = {}
    out = await _post(FakeSession(), monkeypatch, reply="괜찮아.", capture=capture)

    assert out.reply.content == "괜찮아."  # 응답은 살아있어야 함
    last_content = capture["convo"][-1]["content"]
    assert last_content.endswith("오늘 어땠어?")  # 블록 없이 원문만(날짜 표식 제외)
    assert "[지금]" not in last_content
    assert any("현재 턴 컨텍스트" in r.message for r in caplog.records)


# 5) context_ms가 계측 로그에 포함된다 — 비활성(0.0)/활성(측정값) 양쪽.
async def test_context_ms_zero_when_disabled(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="moly-backend")
    await _post(FakeSession(), monkeypatch)
    payload = _turn_metrics_payload(caplog)
    assert payload["context_ms"] == 0.0


async def test_context_ms_measured_when_enabled(monkeypatch, caplog):
    monkeypatch.setattr(chat_service.settings, "current_turn_context_enabled", True)

    async def _fake_build_context(session, profile, *, is_first_today, now_utc):
        return turn_context_module.CurrentTurnContext()

    monkeypatch.setattr(turn_context_module, "build_context", _fake_build_context)
    caplog.set_level(logging.INFO, logger="moly-backend")
    await _post(FakeSession(), monkeypatch)
    payload = _turn_metrics_payload(caplog)
    assert payload["context_ms"] is not None and payload["context_ms"] >= 0

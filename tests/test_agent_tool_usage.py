"""도구를 언제 부르는가 — 규칙이 한 곳에 있고 모든 도구에 걸린다."""
from __future__ import annotations

import pytest


def test_when_to_use_rule_is_attached_to_every_tool_call():
    """도구마다 설명에 적으면 새 도구가 늘 때 빠진다 — 한 곳에서 건다.

    운영 실측(2026-08-08): 규칙이 없어 모델이 말을 잇는 재료로 도구를 아무 때나 부르고,
    한 사용자의 최근 30턴 중 8번 루틴 달성을 먼저 꺼냈다. "그만 말해"라고 한 다음 턴에도 또 꺼냈다.
    """
    import inspect
    from app.services.agent import runtime

    rule = runtime._TOOL_WHEN_RULE
    assert "only to answer what the user just asked" in rule
    assert "small talk" in rule
    assert "says to stop" in rule
    # 실제로 system에 붙는지 — 안 붙으면 규칙이 있으나 마나다.
    src = inspect.getsource(runtime)
    assert "_TOOL_WHEN_RULE]" in src or "_TOOL_WHEN_RULE," in src


def test_persona_forbids_volunteering_routines():
    from app.services import prompts

    for persona, needle in (
        (prompts.CAPI_PERSONA, "상대 루틴을 네가 먼저 꺼내지 마"),
        (prompts.CAPI_PERSONA_JA, "相手のルーティンをきみから先に出さない"),
        (prompts.CAPI_PERSONA_EN, "Never bring up their routines first"),
    ):
        assert needle in persona


# ── 회상 켜짐 판정은 언어별이어야 한다 ────────────────────────
#
# 한국어 기준(6글자)만 쓰던 동안 영어에서 `sounds good`·`good morning` 같은 호응이 전부
# 회상을 켰다. 같은 내용에 영어는 글자가 3배쯤 들어서다. 일본어는 반대로 되짚는 말을 놓쳤다.
@pytest.mark.parametrize("q,expected", [
    ("hi", False), ("haha okay", False), ("sounds good", False), ("good morning", False),
    ("what did I tell you about my job?", True), ("do you remember my dog?", True),
    ("I went to the gym and it was really tough today", True),
])
def test_needs_recall_english(q, expected):
    from app.services import mem0_recall
    assert mem0_recall.needs_recall(q, "en") is expected


@pytest.mark.parametrize("q,expected", [
    ("うん", False), ("そうだね", False), ("おはよう", False),
    ("彼女の名前", True), ("昨日の話", True), ("何食べたっけ", True),
])
def test_needs_recall_japanese(q, expected):
    from app.services import mem0_recall
    assert mem0_recall.needs_recall(q, "ja") is expected


@pytest.mark.parametrize("q,expected", [
    ("안녕", False), ("ㅇㅇ", False),
    ("민승이?", True), ("내 루틴 뭐있었지?", True), ("오늘 힘들었어", True),
])
def test_needs_recall_korean_unchanged(q, expected):
    from app.services import mem0_recall
    assert mem0_recall.needs_recall(q, "ko") is expected


def test_recall_receives_the_language_from_chat():
    """언어를 안 넘기면 전부 영어 기준으로 판정된다(i18n 기본값이 en)."""
    import inspect
    from app.services import chat
    src = inspect.getsource(chat)
    block = src.split("mem0_recall.recall(")[1][:400]
    assert "language=language" in block

"""Development Korean wiring; output quality requires separate blinded trials."""
from unittest.mock import AsyncMock

import pytest

from app.services import chat, mood_context, prompts, turn_context
from tests.test_chat import FakeSession
from tests.test_chat_turn_context_wiring import _post


@pytest.mark.parametrize("mood", ["평범", "unknown"])
@pytest.mark.parametrize("state", [
    None,
    turn_context.CurrentTurnContext(time_bucket="day", routines_planned=2, routines_done=1),
    turn_context.CurrentTurnContext(
        time_bucket="day", equipped_names=["파란 모자"], routines_planned=2, routines_done=1,
    ),
])
async def test_development_persona_keeps_mood_time_and_owned_state_outside_cached_prefix(
    monkeypatch, mood, state,
):
    monkeypatch.setattr(chat.settings, "environment", "development")
    monkeypatch.setattr(chat.settings, "current_turn_context_enabled", state is not None)
    monkeypatch.setattr(mood_context, "today_block", AsyncMock(
        return_value=f"[User mood selection] 2026-10-14: {mood}",
    ))
    builder = AsyncMock(return_value=state)
    monkeypatch.setattr(turn_context, "build_context", builder)
    capture = {}
    await _post(FakeSession(), monkeypatch, capture=capture)
    assert capture["system"][0] == prompts.eval_system_prompt("ko", "voice-structured-ko-v9")[0]
    assert "2026-10-14:" not in "\n".join(capture["system"])
    assert "파란 모자" not in "\n".join(capture["system"])
    assert "오늘 1/2" not in "\n".join(capture["system"])
    conversation = capture["convo"]
    assert conversation[-1]["role"] == "user"
    assert conversation[-1]["content"].endswith("오늘 어땠어?")
    volatile = conversation[-2]
    assert volatile["role"] == "system"
    assert "[Time]" in volatile["content"]
    assert f"[User mood selection] 2026-10-14: {mood}" in volatile["content"]
    assert "[지난 이야기]" not in volatile["content"]
    if state is None:
        builder.assert_not_awaited()
        assert "[지금 상태 - 서버 사실]" not in volatile["content"]
    else:
        builder.assert_awaited_once()
        assert "[지금 상태 - 서버 사실]" in volatile["content"]
        assert "[상대 루틴]" in volatile["content"]
        assert turn_context.render(state, "ko") in volatile["content"]
        # A supplied state with no appearance differs from no state at all.
        assert ("[모습]" in volatile["content"]) is bool(state.equipped_names)
    assert all("[User mood selection]" not in item["content"]
               for item in conversation if item["role"] != "system")

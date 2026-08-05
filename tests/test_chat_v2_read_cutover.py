"""v2 읽기 전환 — 전환되지 않은 사용자는 아무것도 달라지지 않는다.

새 기억을 실제 응답에 쓰기 시작하는 변경이라, 가장 중요한 성질은 "새 것이 잘 도는가"가
아니라 **"안 켠 사용자에게 아무 일도 안 일어나는가"**다. 여기가 깨지면 전환 대상이 아닌
사용자의 대화가 조용히 바뀐다.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.services import chat


class _State:
    def __init__(self, serves_v2: bool):
        self.serves_v2 = serves_v2


def _system(**over):
    kwargs = dict(language="ko", nickname="승민", lead=None,
                  summary="", relationship_text="", current_state="")
    kwargs.update(over)
    return chat._build_system(**kwargs)


# ── 안 켠 사용자는 그대로 ────────────────────────────────────

def test_prompt_is_unchanged_when_v2_block_is_empty():
    """v2 블록이 비면 legacy 경로가 그대로 살아 있어야 한다."""
    before = _system(relationship_text="고양이를 키운다")
    after = _system(relationship_text="고양이를 키운다", memory_v2_block="")
    assert before == after


@pytest.mark.parametrize("mode_serves_v2", [False])
async def test_non_v2_user_gets_empty_block_without_touching_provider(mode_serves_v2):
    """mode가 v2가 아니면 임베딩·벡터 검색을 **아예 부르지 않는다** — 비용과 지연 0."""
    called = {"n": 0}

    async def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("전환 안 된 사용자에게 회상이 돌았다")

    import app.services.chat as c

    original = c._recall_adapter
    c._recall_adapter = _boom
    try:
        got = await chat._recall_memory_v2(
            SimpleNamespace(), query="회사", state=_State(mode_serves_v2), language="ko"
        )
    finally:
        c._recall_adapter = original
    assert got == "" and called["n"] == 0


# ── 켠 사용자 ────────────────────────────────────────────────

def test_v2_block_replaces_legacy_block_not_stacks():
    """둘 다 넣으면 같은 사실이 두 벌로 들어가 캐피가 중복해서 말한다."""
    out = _system(relationship_text="고양이를 키운다", memory_v2_block="[기억]\n- 고양이를 키운다")
    joined = "\n".join(out)
    assert joined.count("고양이를 키운다") == 1


def test_v2_block_is_a_server_owned_system_block():
    """user 발화로 들어가면 사용자 권위를 갖게 된다 — 저장 데이터는 그러면 안 된다."""
    out = _system(memory_v2_block="[기억]\n- 고양이를 키운다")
    assert any("고양이를 키운다" in part for part in out)


def test_nickname_is_rendered_in_the_v2_block():
    """저장은 placeholder로 한다 — 렌더를 빼먹으면 사용자에게 {유저이름}이 그대로 보인다."""
    out = _system(memory_v2_block="[기억]\n- {유저이름}은 고양이를 키운다")
    assert "{유저이름}" not in "\n".join(out)


# ── 실패해도 대화는 살아야 한다 ─────────────────────────────

async def test_recall_failure_does_not_break_the_reply():
    """회상은 보조지 전제가 아니다. 여기서 예외가 새면 응답 전체가 실패한다."""
    import app.services.chat as c

    def _boom():
        raise RuntimeError("vector store down")

    original = c._recall_adapter
    c._recall_adapter = _boom
    try:
        got = await chat._recall_memory_v2(
            SimpleNamespace(), query="회사", state=_State(True), language="ko"
        )
    finally:
        c._recall_adapter = original
    assert got == ""


def test_recall_has_a_timeout():
    """타임아웃이 없으면 벡터 검색 지연이 그대로 챗 응답 지연이 된다."""
    assert chat._MEM0_RECALL_TIMEOUT_S <= 3.0
    src = inspect.getsource(chat._recall_memory_v2)
    assert "timeout=" in src


def test_recall_is_started_early_and_awaited_after_commit():
    """회상을 커밋 뒤에 **직렬로** 부르면 그 지연이 그대로 LLM 예산에서 빠진다.

    실측: 회상 ON 5.7s 타임아웃 / OFF 2.8s 성공. 임베딩+벡터검색 중앙 570ms가 마감을
    넘겼다. 그래서 Phase 1 시작 직후 태스크로 띄워 DB 작업과 겹쳐 돌리고, 커밋 뒤에 거둔다.

    요청 세션을 쥔 채 외부 호출을 기다리는 문제는 **자체 세션**으로 막는다
    (test_recall_uses_its_own_session).
    """
    src = inspect.getsource(chat.post_message)
    start_at = src.index("_recall_memory_v2(")
    commit_at = src.index("await session.commit()")
    await_at = src.index("await recall_task")
    assert start_at < commit_at, "회상을 미리 띄우지 않으면 지연이 숨지 않는다"
    assert await_at > commit_at, "커밋 전에 거두면 겹쳐 돈 의미가 없다"


def test_recall_uses_its_own_session():
    """챗의 세션을 재사용하면 커밋으로 놓은 커넥션을 다시 잡는다."""
    src = inspect.getsource(chat._recall_memory_v2)
    assert "get_sessionmaker()" in src

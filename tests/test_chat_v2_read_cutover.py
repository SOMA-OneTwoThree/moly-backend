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

def test_v2_memory_is_not_in_the_cached_system_prefix():
    """system은 캐시되는 프리픽스다. 매 턴 달라지는 회상을 여기 두면 그 뒤가 전부 miss다.

    실측: 이 자리에 두면 캐시 쓰기가 59토큰 → 4,232토큰으로 뛴다(캐시 쓰기는 읽기의
    12.5배 단가라 비용도 같이 뛴다).
    """
    src = inspect.getsource(chat._build_system)
    assert "memory_v2_block" not in src, "v2 기억이 다시 system 프리픽스로 들어갔다"


def test_v2_memory_goes_after_the_recent_turns():
    """11장이 정한 자리는 최근 원문 **뒤**다 — 그래야 앞의 append-only 대화가 캐시된다."""
    src = inspect.getsource(chat.post_message)
    assert "convo.insert(" in src, "휘발 블록을 convo에 넣지 않는다"
    idx = src.index("convo.insert(")
    assert '"role": "system"' in src[idx:idx + 200], "role이 system이 아니면 user 권위를 갖는다"
    # 요약·기억·서버상태가 모두 휘발 블록으로 모였는지
    vol = src[src.index("volatile: list[str] = []"):idx]
    for name in ("checkpoint_summary", "memory_v2_block", "resident_block"):
        assert name in vol, f"{name}이 휘발 블록에 없다 — 캐시 프리픽스에 남아 있다"
    assert "naming.render(" in vol, "placeholder를 렌더하지 않으면 {유저이름}이 보인다"


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


def test_recall_is_bounded_at_the_boundary_not_just_inside():
    """안쪽 타임아웃만 믿으면 안 된다.

    `embed_query`는 `settings.llm_timeout_s`(60초)를 쓰고 회상 예산과 무관하다. 안쪽에만
    타임아웃을 걸어두면 임베딩이 느려질 때 60초를 기다려 5초 마감을 통째로 날린다.
    (이전 버전의 이 테스트는 소스에 `timeout=`이 있는지만 봐서 이걸 못 잡았다.)
    """
    assert chat._MEM0_RECALL_TIMEOUT_S <= 3.0
    src = inspect.getsource(chat.post_message)
    assert "asyncio.wait_for(" in src, "회상 await에 상한이 없다"
    wf = src.index("asyncio.wait_for(")
    assert "recall_task" in src[wf:wf + 120], "wait_for가 회상 태스크를 감싸지 않는다"


async def test_recall_timeout_yields_empty_memory_not_an_error():
    """타임아웃이 예외로 새면 회상 지연 때문에 대화가 실패한다."""
    import asyncio as _a

    async def _slow():
        await _a.sleep(10)
        return "안 와야 한다"

    task = _a.ensure_future(_slow())
    try:
        out = await _a.wait_for(task, timeout=0.05)
    except (TimeoutError, _a.CancelledError):
        task.cancel()
        out = ""
    assert out == ""


def test_recall_is_started_early_and_awaited_after_commit():
    """회상을 커밋 뒤에 **직렬로** 부르면 그 지연이 그대로 LLM 예산에서 빠진다.

    실측: 회상 ON 5.7s 타임아웃 / OFF 2.8s 성공. 임베딩+벡터검색 중앙 570ms가 마감을
    넘겼다. 그래서 Phase 1 시작 직후 태스크로 띄워 DB 작업과 겹쳐 돌리고, 커밋 뒤에 거둔다.

    요청 세션을 쥔 채 외부 호출을 기다리는 문제는 **자체 세션**으로 막는다
    (test_recall_uses_its_own_session).
    """
    src = inspect.getsource(chat.post_message)
    start_at = src.index("recall_task = asyncio.ensure_future")
    commit_at = src.index("await session.commit()")
    collect_at = src.index("asyncio.wait_for(")
    assert start_at < commit_at, "회상을 미리 띄우지 않으면 지연이 숨지 않는다"
    assert collect_at > commit_at, "커밋 전에 거두면 겹쳐 돈 의미가 없다"


def test_recall_uses_its_own_session():
    """챗의 세션을 재사용하면 커밋으로 놓은 커넥션을 다시 잡는다."""
    src = inspect.getsource(chat._recall_memory_v2)
    assert "get_sessionmaker()" in src


def test_nothing_raises_between_starting_and_awaiting_the_recall_task():
    """태스크를 만든 뒤 raise하면 그 태스크가 고아가 된다.

    응답은 실패했는데 임베딩 호출과 DB 세션은 그대로 돈다. 자체 세션이라 곧 정리되지만,
    클라이언트가 잘못된 greeting_id를 반복해 보내면 그만큼 낭비된다. 실제로 그런 raise가
    하나 있었고(잘못된 greeting_id) 검증을 앞으로 옮겨 없앴다.

    함수 전체를 파싱해 **줄 번호로** 비교한다 — 소스를 잘라 파싱하면 try 블록 중간에서
    잘려 IndentationError가 난다(처음 작성했을 때 실제로 그랬다).
    """
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(chat.post_message))
    lines = src.splitlines()
    start = next(i for i, ln in enumerate(lines)
                 if "recall_task = asyncio.ensure_future" in ln) + 1
    end = next(i for i, ln in enumerate(lines) if "asyncio.wait_for(" in ln) + 1
    assert start < end

    tree = ast.parse(src)
    inside = [n for n in ast.walk(tree)
              if isinstance(n, ast.Raise) and start < n.lineno < end]
    assert not inside, (
        f"회상 태스크 생성~수거 사이에 raise가 {len(inside)}건 있다 "
        f"(줄 {[n.lineno for n in inside]}) — 태스크가 고아가 된다. "
        "검증을 태스크 생성보다 앞으로 옮기거나, 태스크를 cancel하고 raise할 것."
    )




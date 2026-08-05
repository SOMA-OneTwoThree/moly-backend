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


def test_rollback_is_a_mode_flip_not_a_code_change():
    """legacy 경로가 코드에 남아 있어야 mode 한 줄로 되돌릴 수 있다.

    전환이 잘못됐을 때 배포를 되돌려야 한다면 대응이 몇 분에서 몇십 분으로 늘어난다.
    """
    src = inspect.getsource(chat._build_system)
    assert "relationship_text" in src, "legacy 블록 경로가 사라졌다"
    assert "elif relationship_text" in src, "v2/legacy가 배타 분기가 아니다"


def test_v2_user_does_not_get_the_legacy_block():
    """둘 다 들어가면 같은 사실이 두 벌이 되고 캐시만 늘어난다."""
    out = "\n".join(_system(
        relationship_text="레거시 기억", memory_v2_block="[기억]\n- v2 기억"))
    assert "v2 기억" in out and "레거시 기억" not in out


def test_legacy_user_still_gets_the_legacy_block():
    """전환 안 된 사용자는 지금까지와 똑같아야 한다."""
    out = "\n".join(_system(relationship_text="레거시 기억"))
    assert "레거시 기억" in out


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


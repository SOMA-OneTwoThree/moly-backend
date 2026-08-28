"""W11 배선 — post_message가 요약 checkpoint를 만들고(producer) 프롬프트에 쓰는(consumer) 방식.

`checkpoint`/`checkpoint_repo` 자체의 계약은 test_checkpoint*.py가 본다. 여기서 확인하는 건 chat.py
배선뿐이다:

- **킬스위치 off 회귀 0**(기본값, 현재 전 유저) — 잡도 안 생기고 조회도 안 하고 프롬프트도 그대로다.
- 리셋이 일어난 턴에만, 메시지와 **같은 트랜잭션**에서 잡을 건다.
- 요약 경계가 **새 앵커와 정확히 맞물린다**(요약은 앵커 직전까지, 프롬프트 대화는 앵커부터).
  특히 `_keep_window`의 "첫 메시지 user 보장" pop으로 밀려난 캐피 메시지가 요약에 들어간다.
- 요약은 **안정 프리픽스(system 가변 블록)**에 들어가고 현재 닉네임으로 렌더된다.
"""
import uuid
from datetime import date
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.message import Message
from app.services import chat as chat_service
from app.services import checkpoint, checkpoint_repo
from app.services import gating as gating_module
from app.services import llm as llm_module
from app.services.llm import LLMResult
from tests.test_chat import UID, FakeSession, _gating
from tests.test_chat import _Result

_AD = date(2026, 7, 7)


class _Session(FakeSession):
    """commit 횟수를 세고 **messages 조회에만** 세그먼트를 돌려주는 FakeSession.

    기본 FakeSession은 모든 문장에 같은 목록을 준다 — 그러면 `app_config` 조회(도구 루프 설정)까지
    Message 행을 받아 깨진다. 문장별로 갈라 줘야 배선이 실제로 무엇을 조회하는지도 드러난다.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.commits = 0
        self.message_queries = 0

    async def execute(self, stmt, params=None):
        if "FROM messages" in str(stmt):  # 컴파일 문자열은 개행 뒤 FROM
            self.message_queries += 1
            return _Result(self.execute_items)
        return _Result([])

    async def commit(self):
        self.commits += 1
        await super().commit()


def _msg(i: int, sender: str) -> Message:
    m = Message(
        user_id=chat_service._uid(UID), sender=sender, kind="normal",
        content=f"메시지 {i}", activity_date=_AD,
    )
    m.id = i
    return m


def _segment(n: int = 40) -> list[Message]:
    """앵커 이후 세그먼트 n건. FakeSession.execute는 모든 문장에 같은 목록을 주므로
    `_context`(desc 조회)와 배선의 세그먼트 조회(asc)가 같은 값을 본다 — 정렬은
    `checkpoint.normalize_messages`가 하므로 결과는 같다."""
    return [
        _msg(i, "user" if i % 2 else "moly")
        for i in range(n, 0, -1)  # desc — _context가 reversed로 asc를 얻는다
    ]


@pytest.fixture
def spy(monkeypatch) -> dict:
    """producer·consumer 진입점 스파이. 실제 plan 계산은 그대로 돌린다."""
    state: dict = {"enqueued": [], "latest": None, "latest_calls": 0,
                   "systems": [], "convo": []}

    async def _load_latest(session, user_id):
        state["latest_calls"] += 1
        return state["latest"]

    async def _enqueue_checkpoint(session, *, user_id, plan):
        state["enqueued"].append({
            "user_id": user_id, "plan": plan, "commits": session.commits,
        })
        return uuid.uuid4()

    monkeypatch.setattr(checkpoint_repo, "load_latest", _load_latest)
    monkeypatch.setattr(checkpoint_repo, "enqueue_checkpoint", _enqueue_checkpoint)
    return state


async def _post(session, monkeypatch, spy, *, key="idem-w11"):
    async def _res(s, user_id, **kwargs):
        return _gating()

    async def _fake_llm(system, convo, **kw):
        spy["systems"].append(system)
        spy["convo"] = convo
        return LLMResult(text="그랬구나.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    req = SimpleNamespace(text="오늘 이사했어.", greeting_id=None)
    return await chat_service.post_message(session, UID, req, key)


def _system_text(spy) -> str:
    return "\n".join(spy["systems"][0])


def _prompt_text(spy) -> str:
    """system + 휘발 블록 전체. 요약·기억·서버상태는 휘발 블록에 있다."""
    vol = [c["content"] for c in spy.get("convo", []) if c.get("role") == "system"]
    return "\n".join(spy["systems"][0]) + "\n" + "\n".join(vol)


# ─────────────────────────────────────────────────────────────
# 1. 킬스위치 off 회귀 0 — 배포 안전성의 근거(현재 전 유저가 여기 해당).
# ─────────────────────────────────────────────────────────────
async def test_disabled_switch_does_nothing_on_a_resetting_turn(monkeypatch, spy):
    """리셋이 일어나는 턴인데도 잡 0·조회 0·프롬프트 무변화여야 한다."""
    monkeypatch.setattr(settings, "context_checkpoint_enabled", False)
    session = _Session(execute_items=_segment(40))

    out = await _post(session, monkeypatch, spy)

    assert out.reply.content == "그랬구나."
    assert spy["enqueued"] == []       # 잡 0
    assert spy["latest_calls"] == 0    # 조회조차 안 한다
    assert "[지난 이야기]" not in _system_text(spy)
    assert session.commits == 2        # phase1 + phase2 — 커밋 횟수도 그대로


async def test_disabled_switch_ignores_an_existing_checkpoint(monkeypatch, spy):
    """행이 이미 있어도 off면 프롬프트에 새지 않는다(롤백 시 즉시 원복)."""
    monkeypatch.setattr(settings, "context_checkpoint_enabled", False)
    spy["latest"] = checkpoint.Checkpoint(
        id=uuid.uuid4(), through_message_id=21, summary="이사 준비 이야기를 했다",
        version=checkpoint.SUMMARIZER_VERSION, source_hash="a" * 64,
    )
    session = _Session(execute_items=_segment(40))

    await _post(session, monkeypatch, spy)

    assert "이사 준비 이야기를 했다" not in _system_text(spy)
    assert spy["latest_calls"] == 0


# ─────────────────────────────────────────────────────────────
# 2. producer — 리셋 턴에만, 메시지와 같은 트랜잭션에서.
# ─────────────────────────────────────────────────────────────
@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)


async def test_reset_enqueues_a_checkpoint_before_commit(monkeypatch, spy, enabled):
    session = _Session(execute_items=_segment(40))

    await _post(session, monkeypatch, spy)

    assert len(spy["enqueued"]) == 1
    # phase-1 커밋(1회) 직후 = phase 2 트랜잭션 안, 커밋은 그 뒤에 한 번 더.
    assert spy["enqueued"][0]["commits"] == 1
    assert session.commits == 2


async def test_turn_without_reset_enqueues_nothing(monkeypatch, spy, enabled):
    """트리거에 못 닿은 턴 = 버려지는 구간이 없다 → checkpoint도 없다."""
    session = _Session(execute_items=_segment(6))

    await _post(session, monkeypatch, spy)

    assert spy["enqueued"] == []


# ─────────────────────────────────────────────────────────────
# 3. 경계 — 요약은 새 앵커 직전까지, 대화는 새 앵커부터(겹침 0·빈틈 0).
# ─────────────────────────────────────────────────────────────
async def test_summary_boundary_matches_the_new_anchor(monkeypatch, spy, enabled):
    session = _Session(execute_items=_segment(40))

    await _post(session, monkeypatch, spy)

    plan = spy["enqueued"][0]["plan"]
    saved_anchor = plan.through_message_id + 1
    # 보존 tail = 최근 20건(#21~#40) → 새 앵커 #21, 요약은 그 직전(#20)까지.
    assert saved_anchor == 21
    assert plan.source_message_ids == tuple(range(1, 21))


async def test_summary_covers_the_message_popped_by_the_first_user_rule(monkeypatch, spy, enabled):
    """`_keep_window`가 첫-user 보장으로 pop한 캐피 메시지는 **요약이 받아간다**.

    `keep_tail`엔 그 pop 규칙이 없어 경계가 한 칸 어긋날 수 있는데, 배선이 새 앵커를
    `keep_from_message_id`로 넘기므로 두 경계가 자동으로 맞는다 — pop된 #21이 버려지지 않고
    요약 구간에 포함된다.
    """
    # #21(보존 tail의 첫 메시지)만 캐피 메시지, #22부터는 유저 → _keep_window가 #21 하나만 pop.
    items = [_msg(i, "moly" if i == 21 else "user") for i in range(40, 0, -1)]
    session = _Session(execute_items=items)

    await _post(session, monkeypatch, spy)

    plan = spy["enqueued"][0]["plan"]
    assert plan.through_message_id == 21           # pop된 캐피 메시지까지 요약이 덮는다
    assert plan.source_message_ids == tuple(range(1, 22))


async def test_next_segment_starts_after_the_previous_checkpoint(monkeypatch, spy, enabled):
    """이전 checkpoint가 있으면 그 이후만 새로 요약한다(같은 구간 두 번 요약 금지)."""
    spy["latest"] = checkpoint.Checkpoint(
        id=uuid.uuid4(), through_message_id=10, summary="앞부분 요약",
        version=checkpoint.SUMMARIZER_VERSION, source_hash="a" * 64,
    )
    session = _Session(execute_items=_segment(40))

    await _post(session, monkeypatch, spy)

    plan = spy["enqueued"][0]["plan"]
    assert plan.source_message_ids == tuple(range(11, 21))  # #10까지는 이미 덮였다
    assert plan.previous_through_message_id == 10


# ─────────────────────────────────────────────────────────────
# 4. consumer — 요약이 안정 프리픽스(system 가변 블록)에 들어간다.
# ─────────────────────────────────────────────────────────────
async def test_summary_goes_to_the_volatile_block_not_the_cached_prefix(monkeypatch, spy, enabled):
    spy["latest"] = checkpoint.Checkpoint(
        id=uuid.uuid4(), through_message_id=20, summary="이사 준비 이야기를 했다",
        version=checkpoint.SUMMARIZER_VERSION, source_hash="a" * 64,
    )
    session = _Session(execute_items=_segment(6))

    await _post(session, monkeypatch, spy)

    # 요약은 **휘발 블록**이라 system이 아니라 convo 뒤쪽에 들어간다.
    #
    # 예전엔 안정 블록에 뒀다. "요약은 앵커가 움직일 때만 바뀌니 괜찮다"는 가정이었는데,
    # 실측에서 그 가정이 깨졌다 — 요약이 발행된 턴마다 캐시읽기 0 · 쓰기 4,500토큰으로
    # 프롬프트 전체가 다시 청구됐다. prompt_assembly도 CHECKPOINT를 CURRENT로 분류한다.
    convo = spy["convo"]
    blocks = [c["content"] for c in convo if c["role"] == "system"]
    assert any("[지난 이야기]" in b and "이사 준비 이야기를 했다" in b for b in blocks)
    assert all("[지난 이야기]" not in part for part in spy["systems"][0]), \
        "요약이 다시 캐시 프리픽스로 들어갔다"


async def test_summary_is_rendered_with_the_current_nickname(monkeypatch, spy, enabled):
    """저장은 placeholder, 투입은 현재 이름 — 개명해도 과거 요약이 새 이름으로 보인다."""
    spy["latest"] = checkpoint.Checkpoint(
        id=uuid.uuid4(), through_message_id=20, summary="{유저이름}이가 이사 준비를 했다",
        version=checkpoint.SUMMARIZER_VERSION, source_hash="a" * 64,
    )
    session = _Session(execute_items=_segment(6))

    await _post(session, monkeypatch, spy)

    # 요약은 휘발 블록에 있다 — placeholder가 현재 닉네임으로 렌더돼야 한다.
    assert "지훈이가 이사 준비를 했다" in _prompt_text(spy)
    assert "{유저이름}" not in _prompt_text(spy)


async def test_no_checkpoint_leaves_the_prompt_unchanged(monkeypatch, spy, enabled):
    """켜져 있어도 checkpoint가 아직 없으면 블록 자체가 없다(빈 섹션 금지)."""
    session = _Session(execute_items=_segment(6))

    await _post(session, monkeypatch, spy)

    assert "[지난 이야기]" not in _system_text(spy)

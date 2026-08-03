"""W8 배선 — post_message이 정규화 기억 소스(turn·watermark·추출 잡)를 거는 방식.

`memory_repo` 자체의 SQL·불변식은 test_memory_repo.py가 본다. 여기서 확인하는 건 chat.py 배선뿐이다:

- **legacy 유저 회귀 0**(기본값이 legacy라 현재 전 유저) — watermark도 잡도 생기지 않는다.
- normalized 유저는 유저·캐피 메시지와 **같은 트랜잭션**(커밋 전)에 turn 배정 + 추출 잡을 건다.
- 대표 inbound user message가 없는 턴은 조용히 건너뛰고 대화는 정상 응답한다.
- 잡 등록이 DB 오류로 실패하면 그 턴 전체가 롤백된다(부분 성공 없음).
"""
import json
import uuid
from types import SimpleNamespace

import pytest

from app.services import chat as chat_service
from app.services import gating as gating_module
from app.services import jobs
from app.services import llm as llm_module
from app.services import memory as memory_module
from app.services import memory_repo
from app.services.llm import LLMResult
from tests.test_chat import UID, FakeSession, _gating
from tests.test_memory_repo import _Res as _SqlRes

_GREETING_ID = "22222222-2222-2222-2222-222222222222"


class _Session(FakeSession):
    """commit 횟수를 세는 FakeSession — "커밋 전에 썼는가"를 관측하기 위해서다."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.commits = 0

    async def commit(self):
        self.commits += 1
        await super().commit()


def _ctx(mode: str) -> SimpleNamespace:
    return SimpleNamespace(
        anchor_message_id=0, memory_text=None, memory_refreshed_at=None, memory_mode=mode
    )


def _patch_repo(monkeypatch, calls: list, *, alloc_error: Exception | None = None,
                enqueue_error: Exception | None = None):
    async def _alloc(session, *, user_id, representative_message_id, message_ids, committed_at):
        if alloc_error is not None:
            raise alloc_error
        calls.append(
            ("alloc", {
                "user_id": user_id,
                "representative": representative_message_id,
                "message_ids": list(message_ids),
                "committed_at": committed_at,
                "commits": session.commits,
            })
        )
        return memory_repo.SourceTurn(
            watermark=7, memory_generation=3, memory_mode="normalized",
            message_ids=tuple(message_ids),
        )

    async def _enqueue(session, *, user_id, memory_generation, from_watermark,
                       through_watermark, message_ids):
        if enqueue_error is not None:
            raise enqueue_error
        calls.append(
            ("enqueue", {
                "user_id": user_id,
                "memory_generation": memory_generation,
                "from_watermark": from_watermark,
                "through_watermark": through_watermark,
                "message_ids": list(message_ids),
                "commits": session.commits,
            })
        )
        return uuid.uuid4()

    monkeypatch.setattr(memory_repo, "allocate_source_turn", _alloc)
    monkeypatch.setattr(memory_repo, "enqueue_extraction", _enqueue)


async def _post(session, monkeypatch, *, greeting_id=None, key="idem-w8"):
    async def _res(s, user_id):
        return _gating()

    async def _fake_mem(user_id):
        return ""

    async def _fake_llm(system, convo, **kw):
        return LLMResult(text="그랬구나.", input_tokens=10, output_tokens=20)

    monkeypatch.setattr(gating_module, "resolve", _res)
    monkeypatch.setattr(memory_module, "load_for_context", _fake_mem)
    monkeypatch.setattr(llm_module, "generate", _fake_llm)
    req = SimpleNamespace(text="오늘 이사했어.", greeting_id=greeting_id)
    return await chat_service.post_message(session, UID, req, key)


# ─────────────────────────────────────────────────────────────
# 1. legacy 회귀 0 — 이번 작업의 배포 안전성 근거.
# ─────────────────────────────────────────────────────────────
async def test_user_without_chat_context_records_nothing(monkeypatch):
    """chat_contexts 행이 아예 없는 유저(가입 직후) = legacy. 아무 것도 하지 않는다."""
    calls: list = []
    _patch_repo(monkeypatch, calls)
    session = _Session()

    out = await _post(session, monkeypatch)

    assert out.reply.content == "그랬구나."
    assert calls == []


async def test_legacy_mode_records_no_watermark_and_no_job(monkeypatch):
    calls: list = []
    _patch_repo(monkeypatch, calls)
    session = _Session(get_map={"ChatContext": _ctx("legacy")})

    out = await _post(session, monkeypatch)

    assert out.tokens_used == 1110  # 기존 회계 그대로
    assert calls == []
    assert session.commits == 2  # phase1 + phase2 — 커밋 횟수도 그대로


# ─────────────────────────────────────────────────────────────
# 2. normalized — 메시지와 같은 트랜잭션.
# ─────────────────────────────────────────────────────────────
async def test_normalized_allocates_turn_and_enqueues_before_commit(monkeypatch):
    calls: list = []
    _patch_repo(monkeypatch, calls)
    session = _Session(get_map={"ChatContext": _ctx("normalized")})

    await _post(session, monkeypatch)

    kinds = [c[0] for c in calls]
    assert kinds == ["alloc", "enqueue"]
    alloc = calls[0][1]
    # FakeSession.flush가 added 순서대로 id를 준다 — 유저 메시지 1, 캐피 응답 2.
    assert alloc["representative"] == 1
    assert alloc["message_ids"] == [1, 2]
    enqueue = calls[1][1]
    assert enqueue["memory_generation"] == 3
    assert (enqueue["from_watermark"], enqueue["through_watermark"]) == (7, 7)
    assert enqueue["message_ids"] == [1, 2]
    # 둘 다 phase-1 커밋 직후(=phase 2 트랜잭션) 안에서 일어나고, 커밋은 그 뒤에 딱 한 번 더.
    assert alloc["commits"] == 1 and enqueue["commits"] == 1
    assert session.commits == 2


async def test_committed_greeting_joins_the_same_turn(monkeypatch):
    """이 턴에 커밋된 선발화도 같은 watermark에 묶인다(그 인사도 이 대화의 근거다)."""
    calls: list = []
    _patch_repo(monkeypatch, calls)
    greeting = SimpleNamespace(
        user_id=chat_service._uid(UID), committed_message_id=None, content="왔어?"
    )
    session = _Session(
        get_map={"ChatContext": _ctx("normalized"), "Greeting": greeting}
    )

    await _post(session, monkeypatch, greeting_id=_GREETING_ID)

    alloc = calls[0][1]
    assert alloc["message_ids"] == [1, 2, 3]  # 선발화 → 유저 → 캐피
    assert alloc["representative"] == 2       # 대표는 inbound user message
    assert calls[1][1]["message_ids"] == [1, 2, 3]


# ─────────────────────────────────────────────────────────────
# 3. 대표 메시지가 없는 턴 / 실패 정책.
# ─────────────────────────────────────────────────────────────
async def test_turn_without_inbound_user_message_is_skipped_silently(monkeypatch):
    """watermark를 소모하지 않고(allocate가 배정 전에 거부) 대화는 정상 응답한다."""
    calls: list = []
    _patch_repo(
        monkeypatch, calls,
        alloc_error=memory_repo.NoInboundUserMessageError("대표가 user가 아니다"),
    )
    session = _Session(get_map={"ChatContext": _ctx("normalized")})

    out = await _post(session, monkeypatch)

    assert out.reply.content == "그랬구나."  # 대화는 살아 있다
    assert calls == []                        # 추출 잡은 만들지 않는다
    assert session.commits == 2


class _RepoSession(_Session):
    """repo/jobs의 **실제 SQL**을 받아 기록하는 세션.

    다른 테스트는 repo 함수를 갈아끼워 배선만 본다. 그러면 시그니처가 어긋나도(인자 이름 변경 등)
    통과해버리므로, 여기 하나는 진짜 함수를 그대로 태워 어떤 문장이 어느 커밋 경계 안에서
    나가는지까지 확인한다.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.statements: list[tuple[str, object, int]] = []  # (sql, params, 그 시점 commit 수)

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.statements.append((s, params, self.commits))
        if s == str(memory_repo._MESSAGES_SQL):
            return _SqlRes([
                {"id": i, "sender": "user" if i == 1 else "moly", "content": "본문"}
                for i in params["ids"]
            ])
        if s == str(memory_repo._ALLOC_SQL):
            return _SqlRes([(7, 3, "normalized")])
        if s == str(jobs._ENQUEUE_SQL):
            return _SqlRes([(uuid.uuid4(),)])
        return await super().execute(stmt, params)

    def executed(self, sql) -> list[tuple[object, int]]:
        return [(p, c) for s, p, c in self.statements if s == str(sql)]


async def test_real_repo_path_writes_turn_and_job_in_the_phase2_transaction(monkeypatch):
    session = _RepoSession(get_map={"ChatContext": _ctx("normalized")})

    await _post(session, monkeypatch)

    # turn 배정 → turn 메시지 연결 → 추출 잡, 전부 phase-1 커밋 뒤 phase-2 커밋 전(commits==1).
    alloc = session.executed(memory_repo._ALLOC_SQL)
    turn = session.executed(memory_repo._INSERT_TURN_SQL)
    turn_messages = session.executed(memory_repo._INSERT_TURN_MESSAGE_SQL)
    enqueued = session.executed(jobs._ENQUEUE_SQL)
    assert len(alloc) == len(turn) == len(turn_messages) == len(enqueued) == 1
    assert [c for _, c in alloc + turn + turn_messages + enqueued] == [1, 1, 1, 1]
    assert session.commits == 2

    assert turn[0][0]["representative_message_id"] == 1
    assert turn[0][0]["source_watermark"] == 7
    assert [row["message_id"] for row in turn_messages[0][0]] == [1, 2]
    job_params = enqueued[0][0]
    assert job_params["job_type"] == memory_repo.JOB_MEMORY_EXTRACT
    assert job_params["queue"] == jobs.QUEUE_CONTENT
    assert job_params["dedup_key"] == f"{chat_service._uid(UID)}:3:7:7"
    payload = json.loads(job_params["payload"])
    assert payload["source_kind"] == memory_repo.SOURCE_KIND_CONVERSATION_TURN
    assert payload["message_ids"] == [1, 2]


async def test_enqueue_db_failure_rolls_back_the_whole_turn(monkeypatch):
    """같은 트랜잭션이라 fail-open이 성립하지 않는다 — 부분 성공 대신 턴 전체를 되돌린다."""
    calls: list = []
    _patch_repo(monkeypatch, calls, enqueue_error=RuntimeError("insert 실패"))
    session = _Session(get_map={"ChatContext": _ctx("normalized")})

    with pytest.raises(RuntimeError):
        await _post(session, monkeypatch)

    assert session.commits == 1  # phase-1(read-only) 커밋뿐 — 메시지·토큰 저장 0
    assert [c[0] for c in calls] == ["alloc"]

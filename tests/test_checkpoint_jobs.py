"""대화 요약 checkpoint 잡(W11) — 트랜잭션 경계·멱등·확장 금지·킬스위치.

`consumer.run_job`을 그대로 태워서 본다. 잡 행은 test_jobs.py의 in-memory 시뮬레이터를 재사용하고
(`_FakeJobsDB`), 도메인 읽기/쓰기는 `checkpoint_repo`의 함수를 캔 값으로 갈아끼운다 — 여기서
검증하려는 건 SQL이 아니라 **누가 어느 트랜잭션에서 무엇을 하느냐**다.

핵심 관측점:
- checkpoint 저장이 **fenced finalize와 같은 세션·커밋 전**에 일어나는가
- lease를 잃으면 아무것도 저장되지 않는가
- 같은 입력을 다시 실행해도 행이 하나인가(UNIQUE 멱등)
- 🔴 요약에서 **기억 추출 잡이 생기지 않는가**(Summary ≠ Fact)
- 킬스위치 off면 LLM도 쓰기도 0인가
"""
import inspect
import json
import uuid

import pytest

from app.config import settings
from app.services import checkpoint, checkpoint_repo, jobs
from app.services import llm as llm_module
from app.services.jobs import QUEUE_CONTENT
from app.services.llm import LLMResult
from tests.test_jobs import _FakeJobsDB
from tests.test_jobs import _FakeSession as _JobsSession
from worker import checkpoint_jobs, consumer

_UID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_PREV_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")

# 앵커 이전 이력 — 실제 DB에는 남아 있지만 producer의 세그먼트에는 들어오지 않는 구간.
# (앵커 리셋을 겪은 유저는 전부 이 상태다. 핸들러가 여기까지 읽으면 목록이 어긋난다.)
_OLD_HISTORY = [
    checkpoint.SourceMessage(id=i, sender="user" if i % 2 else "moly", kind="normal",
                             content=f"앵커 이전 {i}")
    for i in range(1, 11)
]

# 요약 대상 구간(#11~#13)과 그 앞에 이미 요약된 구간(#1~#10)의 대표 checkpoint.
_SOURCE = [
    checkpoint.SourceMessage(id=11, sender="user", kind="normal", content="{유저이름}이가 이사해"),
    checkpoint.SourceMessage(id=12, sender="moly", kind="normal", content="어디로?"),
    checkpoint.SourceMessage(id=13, sender="user", kind="normal", content="서울"),
]
_PREVIOUS = checkpoint.Checkpoint(
    id=_PREV_ID, through_message_id=10, summary="예전에 이사 얘기를 했다",
    version=checkpoint.SUMMARIZER_VERSION, source_hash="a" * 64,
)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """킬스위치는 기본 off라 잡 테스트는 켜고 시작한다(off 회귀는 전용 테스트가 본다)."""
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)


@pytest.fixture
def db() -> _FakeJobsDB:
    return _FakeJobsDB()


@pytest.fixture(autouse=True)
def _sessions(monkeypatch, db):
    """소비자(finalize)와 핸들러(읽기)가 같은 시뮬레이터를 보게 한다."""
    factory = lambda: _JobsSession(db)  # noqa: E731
    monkeypatch.setattr(consumer, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(checkpoint_jobs, "get_sessionmaker", lambda: factory)


@pytest.fixture
def store(monkeypatch) -> dict:
    """conversation_checkpoints 1테이블의 in-memory 재현 + 유저 상태 캔 값.

    `messages`는 그 유저의 **전체 messages 테이블**이다 — 앵커 이전 이력(#1~#10)이 실제 DB처럼
    그대로 남아 있고, `load_range`는 `(after_id, through_id]`만 걸러 준다. 하한을 편의로 보정하는
    분기를 두면 "핸들러가 앵커 이전까지 읽어버리는" 실제 동작이 테스트에서 사라진다.
    """
    state: dict = {
        "latest": _PREVIOUS,
        "messages": [*_OLD_HISTORY, *_SOURCE],  # = messages 테이블 전체
        "count": 3,
        "nickname": "지훈",
        "language": "ko",
        "user_exists": True,
        "rows": [],             # insert된 checkpoint 행
        "inserts": [],          # 호출 시점의 (세션, 커밋 수)
        "forget_closures": False,   # 잊어줘로 닫힌 구간 존재 여부
        "closed_message_ids": set(),  # 그중 실제로 닫힌 메시지 id
        "memory_generation": 0,     # 현재 기억 세대(forget이 +1 한다)
    }

    async def _latest(session, user_id):
        return state["latest"]

    async def _count(session, user_id):
        return state["count"]

    async def _user_state(session, user_id):
        return (state["nickname"], state["language"]) if state["user_exists"] else None

    async def _range(session, user_id, *, after_id, through_id, max_rows=None):
        """messages 테이블 그대로 — `(after_id, through_id]`를 id 순으로, LIMIT까지."""
        picked = [m for m in state["messages"] if (after_id or 0) < m.id <= through_id]
        return picked[: max_rows] if max_rows is not None else picked

    async def _insert(
        session, *, user_id, through_message_id, summary, source_hash,
        expected_generation, version,
    ):
        state["inserts"].append({
            "session": session, "commits": session.commits,
            "expected_generation": expected_generation,
        })
        # 실제 SQL은 WHERE EXISTS(세대 일치)라 세대가 어긋나면 **아무것도 안 쓴다**.
        if expected_generation != state["memory_generation"]:
            return None
        key = (str(user_id), through_message_id, source_hash)
        if any(r["key"] == key for r in state["rows"]):  # UNIQUE(user, through, source_hash)
            return None
        row = {"key": key, "summary": summary, "version": version}
        state["rows"].append(row)
        return uuid.uuid4()

    monkeypatch.setattr(checkpoint_repo, "load_latest", _latest)
    monkeypatch.setattr(checkpoint_repo, "count", _count)
    monkeypatch.setattr(checkpoint_repo, "load_user_state", _user_state)
    monkeypatch.setattr(checkpoint_repo, "load_range", _range)
    async def _has_closures(session, user_id):
        return state["forget_closures"]

    async def _generation(session, user_id):
        return state["memory_generation"]

    monkeypatch.setattr(checkpoint_repo, "insert", _insert)
    async def _closed_messages(session, user_id, message_ids):
        return state["closed_message_ids"] & set(message_ids)

    monkeypatch.setattr(checkpoint_repo, "has_forget_closures", _has_closures)
    monkeypatch.setattr(checkpoint_repo, "has_closed_messages", _closed_messages)
    monkeypatch.setattr(checkpoint_repo, "read_memory_generation", _generation)
    return state


@pytest.fixture
def llm_calls(monkeypatch) -> list:
    calls: list = []

    async def _generate(system, convo, **kw):
        calls.append({"system": system, "convo": convo, "kw": kw})
        return LLMResult(text="이사 준비 이야기를 나눴다", input_tokens=40, output_tokens=20,
                         model="gpt-5.6-luna")

    monkeypatch.setattr(llm_module, "generate", _generate)
    return calls


def _json(value):
    """jsonb 컬럼 — 실제 드라이버처럼 문자열로 저장되는 경우가 있어 양쪽 모두 dict로 본다."""
    return json.loads(value) if isinstance(value, str) else value


def _payload(*, messages=None, previous=_PREVIOUS, source_hash=None) -> dict:
    msgs = _SOURCE if messages is None else messages
    plan = checkpoint.CheckpointPlan(
        through_message_id=msgs[-1].id,
        source_message_ids=tuple(m.id for m in msgs),
        source_hash=source_hash or checkpoint.source_hash(previous=previous, messages=msgs),
        previous_id=previous.id if previous else None,
        previous_through_message_id=previous.through_message_id if previous else None,
    )
    return plan.payload()


def _enqueue_job(db, payload: dict) -> uuid.UUID:
    return db.insert(
        queue=QUEUE_CONTENT, job_type=checkpoint.JOB_CONVERSATION_CHECKPOINT,
        user_id=_UID, payload=payload,
    )


async def _run(db, jid) -> dict:
    job = next(
        j for j in await jobs.claim(_JobsSession(db), QUEUE_CONTENT, worker_id="W") if j.id == jid
    )
    await consumer.run_job(job, jobs.queue_config(QUEUE_CONTENT))
    return db.rows[jid]


# ─────────────────────────────────────────────────────────────
# 0. 등록
# ─────────────────────────────────────────────────────────────
def test_handler_is_registered():
    assert checkpoint.JOB_CONVERSATION_CHECKPOINT in consumer.registered_types()


def test_consumer_startup_imports_the_checkpoint_handler():
    """등록은 소비자가 기동하며 이 모듈을 import 해야 일어난다.

    위 테스트만 있으면 **이 테스트 파일의 import**가 등록을 대신해줘서 `consumer.run_consumer`의
    import 줄을 지워도 통과한다(워커는 핸들러 없이 뜨는데 테스트는 초록). 그래서 등록이 실제로
    걸리는 지점을 직접 본다.
    """
    assert "_register_handlers()" in inspect.getsource(consumer.run_consumer)
    assert "checkpoint_jobs" in inspect.getsource(consumer._register_handlers)


# ─────────────────────────────────────────────────────────────
# 1. 정상 경로 — fenced finalize 안에서 저장
# ─────────────────────────────────────────────────────────────
async def test_checkpoint_is_written_inside_the_fenced_finalize(db, store, llm_calls):
    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["state"] == "succeeded" and row["result_code"] == checkpoint_jobs.RESULT_OK
    assert len(llm_calls) == 1
    # 저장은 finalize 트랜잭션 안에서(= 아직 커밋 전) 일어난다.
    assert store["inserts"] and store["inserts"][0]["commits"] == 0
    assert len(store["rows"]) == 1
    assert store["rows"][0]["version"] == checkpoint.SUMMARIZER_VERSION


async def test_first_checkpoint_ignores_history_before_the_anchor(db, store, llm_calls):
    """checkpoint 0건 + 앵커 이전 이력이 남아 있는 유저 — 체인의 **첫 마디**가 만들어져야 한다.

    producer가 넘기는 세그먼트는 앵커 이후뿐인데 핸들러가 `(0, through]`를 읽으면 앵커 이전
    이력까지 딸려와 목록이 어긋나고, 다음 트리거에서도 previous=None이라 같은 일이 반복된다
    (= 체인이 영원히 시작되지 않는다). 앵커 리셋을 겪은 유저 전부가 이 상태다.
    """
    store["latest"] = None  # checkpoint 0건

    row = await _run(db, _enqueue_job(db, _payload(previous=None)))

    assert row["state"] == "succeeded" and row["result_code"] == checkpoint_jobs.RESULT_OK
    assert len(store["rows"]) == 1
    body = llm_calls[0]["convo"][0]["content"]
    assert "앵커 이전 10" not in body          # 앵커 이전 이력은 요약 입력이 아니다
    assert "#11" in body and "#13" in body     # 세그먼트만 읽는다


async def test_range_overlapping_the_previous_checkpoint_is_skipped(db, store, llm_calls):
    """이전 checkpoint가 이미 덮은 메시지가 하한에 섞이면 같은 대화를 두 번 요약하게 된다."""
    overlapped = [_OLD_HISTORY[-1], *_SOURCE]  # #10은 이전 checkpoint(through=10)의 구간

    row = await _run(db, _enqueue_job(db, _payload(messages=overlapped)))

    assert row["result_code"] == checkpoint_jobs.RESULT_SOURCE_CHANGED
    assert llm_calls == [] and store["rows"] == []


async def test_nothing_is_written_when_the_lease_is_lost(db, store, llm_calls):
    jid = _enqueue_job(db, _payload())
    job = (await jobs.claim(_JobsSession(db), QUEUE_CONTENT, worker_id="W"))[0]
    db.rows[jid]["lease_token"] = uuid.uuid4()  # 다른 소비자가 이어받았다

    await consumer.run_job(job, jobs.queue_config(QUEUE_CONTENT))

    assert db.rows[jid]["state"] == "running"
    assert store["rows"] == []  # 도메인 반영 0


async def test_rerunning_the_same_input_stays_a_single_row(db, store, llm_calls):
    """같은 입력의 재실행은 UNIQUE(user, through, source_hash)가 흡수한다."""
    payload = _payload()
    await _run(db, _enqueue_job(db, payload))
    store["latest"] = _PREVIOUS  # 앞선 checkpoint가 아직 안 보이는 최악(중복 시도) 상황
    db.rows.clear()
    await _run(db, _enqueue_job(db, payload))

    assert len(store["rows"]) == 1


# ─────────────────────────────────────────────────────────────
# 2. 입력이 달라진 경우 — 요약하지 않는다
# ─────────────────────────────────────────────────────────────
async def test_changed_source_content_is_discarded_without_calling_the_llm(db, store, llm_calls):
    payload = _payload()
    store["messages"] = [
        *_OLD_HISTORY,
        checkpoint.SourceMessage(id=11, sender="user", kind="normal", content="다른 내용"),
        *_SOURCE[1:],
    ]

    row = await _run(db, _enqueue_job(db, payload))

    assert row["state"] == "succeeded"
    assert row["result_code"] == checkpoint_jobs.RESULT_SOURCE_CHANGED
    assert llm_calls == [] and store["rows"] == []


async def test_missing_source_message_is_discarded(db, store, llm_calls):
    payload = _payload()
    store["messages"] = [
        *_OLD_HISTORY, *_SOURCE[:-1],
        checkpoint.SourceMessage(id=99, sender="user", kind="normal", content="딴 메시지"),
    ]

    row = await _run(db, _enqueue_job(db, payload))

    assert row["result_code"] == checkpoint_jobs.RESULT_SOURCE_CHANGED
    assert llm_calls == [] and store["rows"] == []


async def test_already_checkpointed_range_is_skipped(db, store, llm_calls):
    store["latest"] = checkpoint.Checkpoint(
        id=uuid.uuid4(), through_message_id=13, summary="이미 요약됨",
        version=checkpoint.SUMMARIZER_VERSION, source_hash="z" * 64,
    )

    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["result_code"] == checkpoint_jobs.RESULT_ALREADY_CHECKPOINTED
    assert llm_calls == [] and store["rows"] == []


async def test_broken_chain_is_skipped(db, store, llm_calls):
    """그 사이 다른 checkpoint가 들어왔다 = 이 잡의 source_hash 전제가 깨졌다."""
    store["latest"] = checkpoint.Checkpoint(
        id=uuid.uuid4(), through_message_id=10, summary="다른 체인",
        version=checkpoint.SUMMARIZER_VERSION, source_hash="y" * 64,
    )

    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["result_code"] == checkpoint_jobs.RESULT_SOURCE_CHANGED
    assert llm_calls == [] and store["rows"] == []


async def test_deleted_user_is_cancelled(db, store, llm_calls):
    store["user_exists"] = False

    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["state"] == "cancelled" and row["last_error_code"] == "user_deleted"
    assert llm_calls == [] and store["rows"] == []


async def test_bad_payload_is_dead_without_retry(db, store, llm_calls):
    row = await _run(db, _enqueue_job(db, {"schema_version": "nope"}))

    assert row["state"] == "dead" and row["last_error_code"] == "unsupported_payload_schema"
    assert llm_calls == [] and store["rows"] == []


async def test_llm_failure_is_retried(db, store, monkeypatch):
    async def _boom(system, convo, **kw):
        raise TimeoutError("provider timeout")

    monkeypatch.setattr(llm_module, "generate", _boom)

    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["state"] == "ready" and row["last_error_code"] == "summary_llm_failed"
    assert store["rows"] == []


# ─────────────────────────────────────────────────────────────
# 3. Summary ≠ Fact — 요약에서 기억 추출을 만들지 않는다(§W11-7)
# ─────────────────────────────────────────────────────────────
async def test_summary_never_enqueues_a_memory_extraction_job(db, store, llm_calls):
    await _run(db, _enqueue_job(db, _payload()))

    created = {r["job_type"] for r in db.rows.values()}
    assert created == {checkpoint.JOB_CONVERSATION_CHECKPOINT}  # 후속 잡이 하나도 없다


def test_checkpoint_modules_do_not_touch_memory_extraction():
    """소스 수준 방어선 — 요약 경로에서 기억 잡 API를 부르는 코드가 아예 없어야 한다."""
    import inspect

    for module in (checkpoint, checkpoint_repo, checkpoint_jobs):
        src = inspect.getsource(module)
        assert "enqueue_extraction" not in src
        assert "enqueue_reconcile" not in src


# ─────────────────────────────────────────────────────────────
# 4. 저장 표면 — 실명 평문 금지
# ─────────────────────────────────────────────────────────────
async def test_real_name_never_reaches_the_stored_summary(db, store, monkeypatch):
    async def _leaky(system, convo, **kw):
        return LLMResult(text="지훈은 서울로 이사한다", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm_module, "generate", _leaky)

    await _run(db, _enqueue_job(db, _payload()))

    assert len(store["rows"]) == 1
    assert "지훈" not in store["rows"][0]["summary"]
    assert "{유저이름}" in store["rows"][0]["summary"]


async def test_prompt_gets_placeholder_text_not_the_real_name(db, store, llm_calls):
    await _run(db, _enqueue_job(db, _payload()))

    assert "지훈" not in llm_calls[0]["convo"][0]["content"]


# ─────────────────────────────────────────────────────────────
# 5. 재검증 주기 — 매 N번째는 이전 요약 대신 원본으로
# ─────────────────────────────────────────────────────────────
async def test_normal_checkpoint_chains_from_the_previous_summary(db, store, llm_calls):
    store["count"] = 3  # 이번이 4번째

    row = await _run(db, _enqueue_job(db, _payload()))

    assert _json(row["result_detail"])["reverified"] is False
    assert _PREVIOUS.summary in llm_calls[0]["convo"][0]["content"]


async def test_every_nth_checkpoint_reverifies_against_the_originals(db, store, llm_calls):
    store["count"] = settings.context_checkpoint_reverify_every - 1  # 이번이 N번째

    row = await _run(db, _enqueue_job(db, _payload()))

    assert _json(row["result_detail"])["reverified"] is True
    body = llm_calls[0]["convo"][0]["content"]
    assert _PREVIOUS.summary not in body    # 이전 요약을 그대로 물려받지 않는다
    assert "앵커 이전 1" in body            # 대신 경계까지의 원본 전체를 읽는다(superset)


async def test_reverification_is_skipped_when_the_history_is_too_long(db, store, llm_calls, monkeypatch):
    """상한을 넘으면 부분 이력을 전체인 양 요약하지 않고 체인으로 돌아간다."""
    monkeypatch.setattr(settings, "context_checkpoint_reverify_max_messages", 5)
    store["count"] = settings.context_checkpoint_reverify_every - 1

    row = await _run(db, _enqueue_job(db, _payload()))

    assert _json(row["result_detail"])["reverified"] is False
    assert _PREVIOUS.summary in llm_calls[0]["convo"][0]["content"]


# ─────────────────────────────────────────────────────────────
# 6. 킬스위치 — off면 동작 변화 0
# ─────────────────────────────────────────────────────────────
async def test_disabled_switch_makes_the_handler_a_no_op(db, store, llm_calls, monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", False)

    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["state"] == "succeeded"
    assert row["result_code"] == checkpoint_jobs.RESULT_DISABLED
    assert llm_calls == [] and store["rows"] == []


async def test_disabled_switch_enqueues_nothing(db, store, monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", False)
    session = _JobsSession(db)

    segment = [
        checkpoint.SourceMessage(id=i, sender="user", kind="normal", content="가")
        for i in range(1, 41)
    ]
    assert await checkpoint_repo.maybe_enqueue(session, user_id=_UID, messages=segment) is None
    assert db.rows == {}


# ─────────────────────────────────────────────────────────────
# 7. producer — 트리거에 닿았을 때만, 같은 입력이면 한 번만
# ─────────────────────────────────────────────────────────────
async def test_producer_enqueues_only_after_the_trigger(db, store):
    session = _JobsSession(db)
    store["latest"] = None
    under = [
        checkpoint.SourceMessage(id=i, sender="user", kind="normal", content="가")
        for i in range(1, 40)  # 39건 — 트리거 미달
    ]

    assert await checkpoint_repo.maybe_enqueue(session, user_id=_UID, messages=under) is None
    assert db.rows == {}

    segment = under + [
        checkpoint.SourceMessage(id=40, sender="user", kind="normal", content="가")
    ]
    job_id = await checkpoint_repo.maybe_enqueue(session, user_id=_UID, messages=segment)

    assert job_id is not None
    (row,) = list(db.rows.values())
    assert row["job_type"] == checkpoint.JOB_CONVERSATION_CHECKPOINT
    assert row["queue"] == QUEUE_CONTENT
    assert _json(row["payload"])["through_message_id"] == 20
    assert row["dedup_key"].startswith(f"user:{_UID}:through:20:source:")
    assert row["dedup_key"].endswith(f":summarizer:{checkpoint.SUMMARIZER_VERSION}")


async def test_producer_is_idempotent_for_the_same_segment(db, store):
    session = _JobsSession(db)
    store["latest"] = None
    segment = [
        checkpoint.SourceMessage(id=i, sender="user", kind="normal", content="가")
        for i in range(1, 41)
    ]

    first = await checkpoint_repo.maybe_enqueue(session, user_id=_UID, messages=segment)
    second = await checkpoint_repo.maybe_enqueue(session, user_id=_UID, messages=segment)

    assert first is not None and second is None  # (job_type, dedup_key) UNIQUE
    assert len(db.rows) == 1


async def test_producer_does_not_commit(db, store):
    """메시지 insert 트랜잭션에 합류해야 한다 — 여기서 커밋하면 경계가 갈라진다."""
    session = _JobsSession(db)
    store["latest"] = None
    segment = [
        checkpoint.SourceMessage(id=i, sender="user", kind="normal", content="가")
        for i in range(1, 41)
    ]

    await checkpoint_repo.maybe_enqueue(session, user_id=_UID, messages=segment)

    assert session.commits == 0


async def test_reverify_every_one_does_not_brick_the_first_checkpoint(db, store, llm_calls, monkeypatch):
    """reverify_every=1로 둬도 첫 요약이 죽지 않는다.

    재검증은 검증할 체인(previous)이 있을 때만 의미가 있다. previous 없는 첫 요약에서 켜지면
    원본이 비어 빈 입력으로 요약을 시도하고 잡이 재시도 끝에 dead가 된다. 기본값 10에서는
    index%10==0과 previous=None이 양립 불가라 안 드러나지만, 설정 하나로 밟는 지뢰였다.
    """
    monkeypatch.setattr(settings, "context_checkpoint_reverify_every", 1)
    store["latest"] = None  # checkpoint 0건 = 첫 요약

    row = await _run(db, _enqueue_job(db, _payload(previous=None)))

    print("STATE", row["state"], "CODE", row["result_code"], "ERR", row.get("last_error_code"))
    assert row["state"] == "succeeded"


# --- 잊어줘 이후 부활 차단 (적대검증 2라운드 지적) ---
async def test_stale_job_after_forget_writes_nothing(db, store, llm_calls):
    """잊어줘 전에 큐에 들어간 잡이 뒤늦게 실행돼도 요약을 만들지 않는다.

    forget은 checkpoint를 전량 지우고 세대를 +1 한다. 세대 검사가 없으면 지운 직후라
    previous=None이 되어 체인 검사만으로는 "첫 요약"으로 보이고 그냥 통과한다 —
    잊어달라고 한 대화가 요약으로 되살아난다.
    """
    jid = _enqueue_job(db, _payload())      # 잡을 먼저 건다(payload 세대 = 0)
    store["memory_generation"] = 1          # 그 뒤 forget 실행
    store["latest"] = None                  # forget이 checkpoint를 전량 삭제

    row = await _run(db, jid)

    assert row["result_code"] == checkpoint_jobs.RESULT_STALE_GENERATION
    assert store["rows"] == []              # 아무것도 안 썼다
    assert llm_calls == []                  # LLM도 안 불렀다


async def test_reverification_is_skipped_when_forget_closed_a_range(db, store, llm_calls):
    """잊어줘가 닫은 구간이 있으면 재검증하지 않는다.

    재검증은 `(0, through]` 원본 **전체**를 다시 읽는다. 원본 messages에는 잊은 내용이 그대로
    남아 있으므로, 그대로 두면 새 요약으로 재유입된다. 체인 요약으로 폴백한다.
    """
    store["count"] = settings.context_checkpoint_reverify_every - 1  # 이번이 N번째
    store["forget_closures"] = True

    row = await _run(db, _enqueue_job(db, _payload()))

    assert _json(row["result_detail"])["reverified"] is False
    body = llm_calls[0]["convo"][0]["content"]
    assert "앵커 이전 1" not in body        # 원본 전체를 읽지 않았다
    assert _PREVIOUS.summary in body        # 체인 요약으로 갔다


async def test_normal_checkpoint_skips_when_sources_were_closed_by_forget(db, store, llm_calls):
    """잊어줘 이후 만들어진 **일반 요약**도 닫힌 메시지를 담으면 안 된다.

    forget은 앵커를 전진시키지 않는다. 그래서 잊기 이후에 만들어진 잡은 세대가 최신이라
    세대 검사를 통과하고, closure 가드가 재검증에만 걸려 있으면 그대로 통과한다 —
    현재 앵커 이후 구간에 남아 있는 잊기 이전 메시지가 요약으로 되살아난다.
    """
    store["closed_message_ids"] = {12}  # 세그먼트 중 하나가 잊어줘로 닫혔다

    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["result_code"] == checkpoint_jobs.RESULT_SOURCE_CLOSED
    assert store["rows"] == []   # 저장 0
    assert llm_calls == []       # LLM도 안 불렀다


async def test_normal_checkpoint_proceeds_when_nothing_was_closed(db, store, llm_calls):
    """닫힌 게 없으면 종전대로 요약한다(회귀 0)."""
    row = await _run(db, _enqueue_job(db, _payload()))

    assert row["result_code"] == checkpoint_jobs.RESULT_OK
    assert len(store["rows"]) == 1


async def test_insert_carries_the_expected_generation(db, store, llm_calls):
    """저장이 세대를 **같은 문장 안에서** 검사한다(읽고 나서 쓰는 경합을 잠금 없이 없앤다)."""
    await _run(db, _enqueue_job(db, _payload()))

    assert store["inserts"], "저장이 시도돼야 한다"
    assert store["inserts"][-1]["expected_generation"] == 0  # payload 세대를 그대로 넘긴다


async def test_forget_between_check_and_insert_writes_nothing(db, store, llm_calls):
    """검사를 통과한 뒤 저장 직전에 잊어줘가 끼어도 아무것도 안 쓴다.

    세대 검사가 저장과 **한 문장**이라 그 사이가 없다. 따로 읽고 나서 쓰면 그 틈에
    잊어줘가 세대 증가·요약 삭제를 마치고, 저장은 검사를 전부 피해 지운 요약을 되살린다.
    """
    jid = _enqueue_job(db, _payload())          # payload 세대 = 0

    async def _slip(*a, **kw):
        store["memory_generation"] = 1          # 검사 통과 후, 저장 직전에 잊어줘 발생
        return []

    import worker.checkpoint_jobs as cj
    orig = cj.checkpoint_repo.has_closed_messages
    cj.checkpoint_repo.has_closed_messages = _slip
    try:
        await _run(db, jid)
    finally:
        cj.checkpoint_repo.has_closed_messages = orig

    assert store["rows"] == []                  # 저장 0


async def test_unmapped_messages_are_not_collaterally_suppressed(monkeypatch):
    """정확한 suppression 행이 없는 메시지는 다른 망각 이력 때문에 숨기지 않는다.

    매핑 조회는 memory_source_turn_messages에서 출발하는 join이라 매핑 없는 메시지는
    join에 안 걸려 "안전"으로 보인다. 그런 메시지는 실제로 생긴다(Phase 1이 legacy를 읽은 뒤
    cutover되고 Phase 2가 도는 턴). 정체를 모르는 걸 안전으로 넘기면 잊은 내용이 요약에 들어간다.
    """
    calls: list[str] = []

    class _S:
        async def execute(self, stmt, params=None):
            s = str(stmt)
            calls.append(s)
            if "memory_source_closures" in s and "memory_source_turn_messages" not in s:
                return _Rows([(1,)])          # 잊기 이력 있음
            if "JOIN memory_source_closures" in s:
                return _Rows([])              # 닫힌 구간에 직접 걸린 건 없음
            return _Rows([])                  # 매핑도 없음 ← 여기가 핵심

    class _Rows:
        def __init__(self, rows): self._rows = rows
        def first(self): return self._rows[0] if self._rows else None
        def scalars(self): return self
        def all(self): return [r[0] for r in self._rows]

    assert await checkpoint_repo.has_closed_messages(_S(), uuid.uuid4(), [11, 12]) is False

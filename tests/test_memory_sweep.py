"""멈춘 기억 파이프라인 자동 재개.

여기서 지키는 것은 하나다: **죽은 잡은 replay로만 되살릴 수 있다.**

`ingest_dedup_key`는 (user, turn)만으로 정해지고 `enqueue`의 ON CONFLICT는 잡 상태와 무관하게
영구적이다. 그래서 turn N의 잡이 한 번 죽으면 어떤 코드도 그 turn을 다시 걸 수 없고, chat은
`ingest >= source`일 때만 새 잡을 걸므로 그 사용자의 기억은 **영원히 멈춘다**. 증상은 에러가
아니라 침묵이라 손으로 찾기 전에는 드러나지 않는다.

sweep이 `enqueue` 대신 `replay_dead`를 쓰는 이유가 이것이다. 이 파일은 그 선택이 코드에서
유지되는지 본다 — dev 실 DB에서 재개까지 확인한 것은 별도다(세션 기록).
"""
from __future__ import annotations

import inspect
import uuid

from worker import memory_sweep_jobs as sweep


def test_sweep_uses_replay_not_enqueue():
    """`enqueue_ingest`로 되살리려 하면 dedup 벽에 막혀 **아무 일도 일어나지 않는다**.

    실측: 죽은 turn에 `enqueue_ingest`를 다시 부르면 None이 돌아온다.
    """
    src = inspect.getsource(sweep)
    assert "replay_dead" in src, "sweep이 replay를 쓰지 않는다 — dedup 벽을 못 넘는다"
    assert "enqueue_ingest" not in src, (
        "sweep이 enqueue_ingest를 쓴다. 죽은 turn은 dedup_key가 같아 재등록이 조용히 무시된다"
    )


def test_sweep_only_targets_users_whose_cursor_is_behind():
    """커서가 따라잡힌 사용자는 잡이 죽었어도 되살릴 이유가 없다 — 체인이 이미 지나갔다."""
    sql = str(sweep._STALLED)
    assert "ingest_through_turn_seq < s.source_through_turn_seq" in sql
    assert "state = 'dead'" in sql
    assert "bootstrap_status = 'ready'" in sql, "bootstrap 미완 사용자는 live ingest 대상이 아니다"


def test_sweep_skips_already_replayed_jobs():
    """같은 잡을 매 틱 다시 replay하면 잡이 무한히 불어난다."""
    sql = str(sweep._STALLED)
    assert "r.replay_of = j.id" in sql
    assert "'ready', 'running', 'succeeded'" in sql


def test_replay_operation_id_is_deterministic():
    """operation_id가 매번 달라지면 멱등 가드가 무의미해진다."""
    jid = uuid.UUID("11111111-2222-3333-4444-555555555555")
    a = uuid.uuid5(sweep._REPLAY_NAMESPACE, str(jid))
    b = uuid.uuid5(sweep._REPLAY_NAMESPACE, str(jid))
    assert a == b
    assert a != uuid.uuid5(sweep._REPLAY_NAMESPACE, str(uuid.uuid4()))


def test_sweep_is_registered_with_the_consumer():
    """핸들러가 등록되지 않으면 잡은 만들어지고 아무도 집어가지 않는다."""
    from worker import consumer

    consumer._register_handlers()
    assert sweep.JOB_MEMORY_SWEEP in consumer._REGISTRY, (
        f"{sweep.JOB_MEMORY_SWEEP}가 consumer._register_handlers의 import 목록에 없다 — "
        "잡은 만들어지지만 아무도 집어가지 않는다"
    )


# --- 색인 누락 복구가 upsert보다 느슨하면 안 된다 ---
#
# sweep이 "회상 문서가 없는 일기"를 찾아 문서를 만든다. 이 조건이 upsert보다 느슨하면
# 계정 삭제가 진행 중인 사용자의 일기를 되살린다. 그래서 두 조건을 대조로 고정한다.


def _norm(stmt) -> str:
    return " ".join(str(stmt).split())


def test_missing_document_requires_the_same_guards_as_upsert() -> None:
    from app.services import diary_recall_repo
    from worker import memory_sweep_jobs

    upsert = _norm(diary_recall_repo._UPSERT_DOCUMENT)
    sweep = _norm(memory_sweep_jobs._MISSING_DOCUMENT)

    # upsert가 거는 안전 조건은 sweep도 반드시 걸어야 한다.
    assert "record_status='published'" in upsert
    assert "record_status='published'" in sweep, "발행 안 된 일기를 색인한다"

    assert "deleted_at IS NULL" in upsert
    assert "deleted_at IS NULL" in sweep, "삭제된 일기를 색인한다"

    # 계정 삭제 장벽 — 여기가 어긋나면 삭제 중인 사용자의 일기가 되살아난다.
    assert "privacy_subject_barriers" in upsert
    assert "privacy_subject_barriers" in sweep, "삭제 장벽을 보지 않는다"
    assert "state <> 'active'" in upsert
    assert "state <> 'active'" in sweep, "삭제 장벽 판정이 upsert와 다르다"


def test_missing_document_only_targets_diaries_without_a_document() -> None:
    """이미 문서가 있는 일기를 다시 만들면 매 틱 같은 일을 반복한다."""
    from worker import memory_sweep_jobs

    sweep = _norm(memory_sweep_jobs._MISSING_DOCUMENT)
    assert "NOT EXISTS" in sweep and "diary_recall_documents" in sweep


def test_private_repair_excludes_valid_shared_ready_and_pending_rows() -> None:
    sql = _norm(sweep._MISSING_EMBEDDING)
    assert "rd.embedding IS NULL" in sql
    assert "NOT EXISTS" in sql and "diary_template_embeddings se" in sql
    for field in ("source_hash", "embedding_model", "index_version", "search_text"):
        assert f"se.{field}=rd.{field}" in sql
    assert "se.id=rd.shared_embedding_id" in sql and "se.dimensions=1536" in sql
    # A pending or exhausted shared row has a global budget, not one per owner.
    valid_shared = sweep._VALID_SHARED_DOCUMENT
    assert "se.embedding IS NOT NULL" not in valid_shared
    assert "embedding_repair_attempts" not in valid_shared
    assert "owned_diary.kind IN ('welcome','capi_day')" in valid_shared
    assert "record_status='published'" in sql and "privacy_subject_barriers" in sql


def test_private_repair_rechecks_shared_identity_privacy_and_attempt_on_write() -> None:
    sql = _norm(sweep._BUMP_REPAIR_BATCH)
    assert "rd.embedding IS NULL" in sql
    assert "rd.embedding_repair_attempts=t.attempt" in sql
    assert "NOT EXISTS" in sql and "diary_template_embeddings se" in sql
    assert "record_status='published'" in sql and "deleted_at IS NULL" in sql
    assert "privacy_subject_barriers" in sql and "state <> 'active'" in sql


def test_shared_repair_is_bounded_but_has_no_age_cutoff() -> None:
    sql = _norm(sweep._MISSING_SHARED_EMBEDDING)
    assert "se.embedding IS NULL" in sql
    assert "se.embedding_repair_attempts < :max_attempts" in sql
    assert "ORDER BY se.updated_at, se.id LIMIT :limit" in sql
    assert "interval" not in sql.lower()
    assert "j.state IN ('ready','running')" in sql
    assert "j.user_id IS NULL" in sql
    bump = _norm(sweep._BUMP_SHARED_REPAIR_BATCH)
    assert "se.embedding_repair_attempts=t.attempt" in bump
    assert "se.embedding IS NULL" in bump
    assert "j.state IN ('ready','running')" in bump


async def test_shared_repairs_have_no_user_owner_and_only_metadata_payload(monkeypatch) -> None:
    shared_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    captured = {}

    class Result:
        def all(self):
            # Only one row survived the bump CAS: the other was filled meanwhile.
            return [(shared_id, 2)]

    class Session:
        async def execute(self, stmt, params=None):
            captured["stmt"] = stmt
            captured["params"] = params
            return Result()

    async def enqueue_many(session, **kwargs):
        captured.update(kwargs)
        return len(kwargs["items"])

    monkeypatch.setattr(sweep.jobs, "enqueue_many", enqueue_many)
    made = await sweep._enqueue_shared_repairs(Session(), [(shared_id, 1), (stale_id, 0)])
    assert made == 1
    assert captured["params"]["repair_attempts"] == [1, 0]
    assert captured["job_type"] == "diary_template_embed"
    owner, key, payload = captured["items"][0]
    assert owner is None
    assert key == f"diarytemplate:repair:{shared_id}:2"
    assert payload == {
        "shared_id": str(shared_id), "schema_version": sweep.diary_recall_repo.INDEX_VERSION,
    }


async def test_empty_shared_repair_batch_does_not_touch_db() -> None:
    class Session:
        async def execute(self, *args, **kwargs):
            raise AssertionError("no pending shared rows")

    assert await sweep._enqueue_shared_repairs(Session(), []) == 0


async def test_document_repair_claims_unique_users_in_order_without_waiting() -> None:
    first, busy = sorted([uuid.uuid4(), uuid.uuid4()], key=str)
    calls = []

    class Session:
        async def scalar(self, stmt, params):
            calls.append((str(stmt), params["user_id"]))
            return params["user_id"] != str(busy)

    locked = await sweep._try_lock_document_users(
        Session(), [(busy, uuid.uuid4()), (first, uuid.uuid4()), (first, uuid.uuid4())],
    )
    assert locked == {first}
    assert [uid for _sql, uid in calls] == [str(first), str(busy)]
    assert all("pg_try_advisory_xact_lock" in sql for sql, _uid in calls)
    assert all("hashtextextended(:user_id, 0)" in sql for sql, _uid in calls)


async def test_empty_document_repair_does_not_take_user_locks() -> None:
    class Session:
        async def scalar(self, *args, **kwargs):
            raise AssertionError("no documents need recovery")

    assert await sweep._try_lock_document_users(Session(), []) == set()


async def test_sweep_takes_document_user_locks_before_row_and_job_writes(monkeypatch) -> None:
    busy, available = sorted([uuid.uuid4(), uuid.uuid4()], key=str)
    undocumented = [(busy, uuid.uuid4()), (available, uuid.uuid4())]
    sid = uuid.uuid4()
    events = []

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class ScanSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def execute(self, stmt, params=None):
            if stmt is sweep._MISSING_DOCUMENT:
                return Result(undocumented)
            if stmt is sweep._MISSING_EMBEDDING:
                return Result([(busy, uuid.uuid4(), 0)])
            if stmt is sweep._MISSING_SHARED_EMBEDDING:
                return Result([(sid, 0)])
            return Result([])

        async def scalar(self, *args, **kwargs):
            return None

    class ApplySession:
        async def scalar(self, stmt, params):
            assert stmt is sweep._TRY_DOCUMENT_USER_LOCK
            events.append(("user_lock", params["user_id"]))
            return params["user_id"] == str(available)

        async def execute(self, stmt, params=None):
            events.append(("row_write",))
            return Result([])

    async def enqueue_many(*args, **kwargs):
        events.append(("job_write",))
        return 0

    async def replay(*args, **kwargs):
        events.append(("job_write",))
        return set()

    async def upsert(session, *, user_id, diary_id):
        assert (user_id, diary_id) == undocumented[1]
        events.append(("document_upsert", str(user_id)))

    monkeypatch.setattr(sweep, "get_sessionmaker", lambda: ScanSession)
    monkeypatch.setattr(sweep.jobs, "enqueue_many", enqueue_many)
    monkeypatch.setattr(sweep.jobs, "replay_dead_many", replay)
    monkeypatch.setattr(sweep.diary_recall_repo, "upsert_diary_recall_document", upsert)
    result = await sweep.handle_memory_sweep(None)
    await result.apply_domain(ApplySession())
    assert events[:2] == [("user_lock", str(busy)), ("user_lock", str(available))]
    assert not any(event[0] == "user_lock" for event in events[2:])
    assert [event for event in events if event[0] == "document_upsert"] == [
        ("document_upsert", str(available)),
    ]


async def test_private_repair_skips_existing_job_after_source_counter_reset(monkeypatch) -> None:
    uid, old_diary, new_diary = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    existing_key = f"diaryembed:repair:{old_diary}:1"
    captured = {}

    class Result:
        def all(self):
            return [(existing_key,)]

    class Session:
        async def execute(self, stmt, params):
            captured["lookup"] = str(stmt)
            captured["params"] = params
            return Result()

    async def enqueue_many(session, **kwargs):
        captured.update(kwargs)
        return len(kwargs["items"])

    monkeypatch.setattr(sweep.jobs, "enqueue_many", enqueue_many)
    assert await sweep._enqueue_private_repairs(
        Session(), [(uid, old_diary, 1), (uid, new_diary, 1)],
    ) == 1
    assert captured["lookup"].lstrip().startswith("SELECT dedup_key")
    assert "FOR UPDATE" not in captured["lookup"]
    assert captured["params"]["job_type"] == "diary_recall_embed"
    assert captured["items"] == [
        (uid, f"diaryembed:repair:{new_diary}:1",
         {"schema_version": sweep.diary_recall_repo.INDEX_VERSION, "diary_id": str(new_diary)}),
    ]


async def test_private_repair_all_duplicates_perform_no_enqueue(monkeypatch) -> None:
    uid, did = uuid.uuid4(), uuid.uuid4()

    class Result:
        def all(self):
            return [(f"diaryembed:repair:{did}:1",)]

    class Session:
        async def execute(self, *args, **kwargs):
            return Result()

    async def enqueue_many(*args, **kwargs):
        raise AssertionError("committed duplicate must not reach unique-index insertion")

    monkeypatch.setattr(sweep.jobs, "enqueue_many", enqueue_many)
    assert await sweep._enqueue_private_repairs(Session(), [(uid, did, 1)]) == 0


async def test_empty_private_repair_batch_does_not_query_or_enqueue() -> None:
    class Session:
        async def execute(self, *args, **kwargs):
            raise AssertionError("empty batch must not query")

    assert await sweep._enqueue_private_repairs(Session(), []) == 0

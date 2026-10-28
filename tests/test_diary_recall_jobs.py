"""일기 회상 임베딩 처리기 — 등록과 처리 조건.

이 처리기는 오래 **없었다.** 잡을 만드는 코드만 있고 처리기가 없어서 잡이 집어 가는 즉시
죽었고, `diary_recall_documents.embedding`이 한 건도 만들어지지 않았다. 그래서 등록 자체를
테스트로 고정한다.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.services import diary_recall_repo
from app.services.jobs import ClaimedJob
from worker import consumer, diary_recall_jobs


_UNSET = object()  # user_id=None을 "미지정"과 구분하기 위한 표식


def _job(payload: dict, *, user_id=_UNSET) -> ClaimedJob:
    return ClaimedJob(
        id=uuid.uuid4(),
        queue="content",
        job_type=diary_recall_jobs.JOB_DIARY_RECALL_EMBED,
        user_id=uuid.uuid4() if user_id is _UNSET else user_id,
        dedup_key="d",
        payload=payload,
        attempt=1,
        max_attempts=3,
        lease_owner="W",
        lease_token=uuid.uuid4(),
        lease_until=datetime.now(timezone.utc) + timedelta(seconds=60),
        expires_at=None,
    )


def test_handler_is_registered():
    consumer._register_handlers()
    assert diary_recall_jobs.JOB_DIARY_RECALL_EMBED in consumer.registered_types()


def test_handler_registered_on_the_live_consumer_module():
    """소비자 모듈이 두 번 적재되면 실제 registry가 비는 사고가 있었다(PR #91)."""
    consumer._register_handlers()
    assert (
        consumer._REGISTRY.get(diary_recall_jobs.JOB_DIARY_RECALL_EMBED)
        is diary_recall_jobs.handle_diary_recall_embed
    )


def test_job_type_matches_the_enqueue_side():
    """만드는 쪽과 처리하는 쪽이 같은 문자열을 써야 한다."""
    assert (
        diary_recall_jobs.JOB_DIARY_RECALL_EMBED
        == diary_recall_repo.JOB_DIARY_RECALL_EMBED
    )


async def test_missing_user_is_fatal():
    with pytest.raises(consumer.JobFatal):
        await diary_recall_jobs.handle_diary_recall_embed(
            _job({"diary_id": str(uuid.uuid4())}, user_id=None)
        )


@pytest.mark.parametrize("payload", [{}, {"diary_id": "그냥 글자"}, {"diary_id": None}])
async def test_broken_payload_is_fatal_not_retried(payload):
    """형식이 깨진 건 다시 시도해도 같다 — 즉시 끝낸다."""
    with pytest.raises(consumer.JobFatal):
        await diary_recall_jobs.handle_diary_recall_embed(_job(payload))


async def test_not_eligible_document_ends_without_calling_the_embedder(monkeypatch):
    """비공개·삭제된 일기는 실패가 아니다. 임베딩을 부르지도 않는다."""
    called = False

    async def _load(session, *, user_id, diary_id):
        return None

    async def _embed(value):
        nonlocal called
        called = True
        return [0.0]

    monkeypatch.setattr(diary_recall_repo, "embed_diary_document", _load)
    monkeypatch.setattr(diary_recall_repo, "embed_text", _embed)

    result = await diary_recall_jobs.handle_diary_recall_embed(
        _job({"diary_id": str(uuid.uuid4())})
    )
    assert result.result_code == "not_eligible"
    assert result.apply_domain is None
    assert called is False


async def test_embedder_failure_is_retryable(monkeypatch):
    """제공자 장애는 간격을 두고 다시 시도해야 한다 — 즉시 죽이면 그 일기는 영영 안 걸린다."""

    async def _load(session, *, user_id, diary_id):
        return ("본문", 0, "hash", "model", diary_recall_repo.INDEX_VERSION)

    async def _embed(value):
        raise RuntimeError("provider down")

    monkeypatch.setattr(diary_recall_repo, "embed_diary_document", _load)
    monkeypatch.setattr(diary_recall_repo, "embed_text", _embed)

    with pytest.raises(consumer.JobRetry):
        await diary_recall_jobs.handle_diary_recall_embed(
            _job({"diary_id": str(uuid.uuid4())})
        )


async def test_write_happens_in_apply_domain_not_during_the_external_call(monkeypatch):
    """쓰기는 확정 트랜잭션에서만 한다. 임베딩을 부르는 동안 DB를 쥐지 않는다."""
    order: list[str] = []

    async def _load(session, *, user_id, diary_id):
        order.append("load")
        return ("본문", 3, "hash", "model", diary_recall_repo.INDEX_VERSION)

    async def _embed(value):
        order.append("embed")
        return [0.1, 0.2]

    captured: dict = {}

    async def _write(session, **kwargs):
        order.append("write")
        captured.update(kwargs)
        return True

    monkeypatch.setattr(diary_recall_repo, "embed_diary_document", _load)
    monkeypatch.setattr(diary_recall_repo, "embed_text", _embed)
    monkeypatch.setattr(diary_recall_repo, "write_diary_embedding", _write)

    diary_id = uuid.uuid4()
    job = _job({"diary_id": str(diary_id)})
    result = await diary_recall_jobs.handle_diary_recall_embed(job)

    # 처리기가 끝난 시점까지 쓰기는 일어나지 않았다.
    assert order == ["load", "embed"]
    assert result.result_code == "ok"
    assert result.apply_domain is not None

    await result.apply_domain(object())
    assert order == ["load", "embed", "write"]
    # 읽을 때의 좌표를 그대로 넘겨야 그 사이 일기가 바뀐 경우를 걸러낼 수 있다.
    assert captured["user_id"] == job.user_id
    assert captured["diary_id"] == diary_id
    assert captured["source_hash"] == "hash"
    assert captured["generation"] == 3
    assert captured["vector"] == [0.1, 0.2]


async def test_stale_write_does_not_raise(monkeypatch):
    """임베딩을 만드는 사이 일기가 바뀌면 0행이다. 그 수정이 이미 새 잡을 만들었으므로 오류가 아니다."""

    async def _load(session, *, user_id, diary_id):
        return ("본문", 0, "hash", "model", diary_recall_repo.INDEX_VERSION)

    async def _embed(value):
        return [0.1]

    async def _write(session, **kwargs):
        return False

    monkeypatch.setattr(diary_recall_repo, "embed_diary_document", _load)
    monkeypatch.setattr(diary_recall_repo, "embed_text", _embed)
    monkeypatch.setattr(diary_recall_repo, "write_diary_embedding", _write)

    result = await diary_recall_jobs.handle_diary_recall_embed(
        _job({"diary_id": str(uuid.uuid4())})
    )
    await result.apply_domain(object())


class _SessionScope:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        self.events.append("session_open")
        return object()

    async def __aexit__(self, *args):
        self.events.append("session_closed")


def _shared_source():
    return {
        "id": uuid.uuid4(),
        "search_text": "approved public copy",
        "source_hash": "frozen-source-hash",
        "embedding_model": diary_recall_jobs.settings.embedder_model,
        "index_version": diary_recall_repo.INDEX_VERSION,
        "dimensions": diary_recall_jobs.settings.memory_embedding_dimensions,
    }


def test_shared_handler_is_registered_on_the_live_consumer():
    consumer._register_handlers()
    assert (
        consumer._REGISTRY.get(diary_recall_jobs.shared.JOB_DIARY_TEMPLATE_EMBED)
        is diary_recall_jobs.handle_diary_template_embed
    )


async def test_shared_job_rejects_an_owner_before_loading_any_copy(monkeypatch):
    from unittest.mock import AsyncMock

    load = AsyncMock()
    monkeypatch.setattr(diary_recall_jobs.shared, "load_shared", load)
    with pytest.raises(consumer.JobFatal, match="shared_job_has_owner"):
        await diary_recall_jobs.handle_diary_template_embed(_job({"shared_id": str(uuid.uuid4())}))
    load.assert_not_awaited()


@pytest.mark.parametrize("payload", [{}, {"shared_id": None}, {"shared_id": "bad"}])
async def test_shared_job_rejects_invalid_id_before_opening_a_session(monkeypatch, payload):
    def unexpected():
        raise AssertionError("bad payload must not touch the database")

    monkeypatch.setattr(diary_recall_jobs, "get_sessionmaker", unexpected)
    with pytest.raises(consumer.JobFatal, match="invalid_shared_id"):
        await diary_recall_jobs.handle_diary_template_embed(_job(payload, user_id=None))


async def test_already_ready_shared_source_skips_provider_and_write(monkeypatch):
    from unittest.mock import AsyncMock

    events = []
    monkeypatch.setattr(diary_recall_jobs, "get_sessionmaker", lambda: lambda: _SessionScope(events))
    monkeypatch.setattr(diary_recall_jobs.shared, "load_shared", AsyncMock(return_value=None))
    embed, write = AsyncMock(), AsyncMock()
    monkeypatch.setattr(diary_recall_repo, "embed_text", embed)
    monkeypatch.setattr(diary_recall_jobs.shared, "write_shared", write)
    result = await diary_recall_jobs.handle_diary_template_embed(
        _job({"shared_id": str(uuid.uuid4())}, user_id=None),
    )
    assert result.result_code == "already_ready_or_missing"
    assert result.apply_domain is None
    assert events == ["session_open", "session_closed"]
    embed.assert_not_awaited()
    write.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value",
    [("embedding_model", "unsupported-model"), ("dimensions", 32), ("index_version", "future-index")],
)
async def test_shared_job_rejects_unsupported_source_before_provider(monkeypatch, field, value):
    from unittest.mock import AsyncMock

    source, events = _shared_source(), []
    source[field] = value
    monkeypatch.setattr(diary_recall_jobs, "get_sessionmaker", lambda: lambda: _SessionScope(events))
    monkeypatch.setattr(diary_recall_jobs.shared, "load_shared", AsyncMock(return_value=source))
    embed = AsyncMock()
    monkeypatch.setattr(diary_recall_repo, "embed_text", embed)
    with pytest.raises(consumer.JobFatal, match="unsupported_shared_embedding_version"):
        await diary_recall_jobs.handle_diary_template_embed(
            _job({"shared_id": str(source["id"])}, user_id=None),
        )
    embed.assert_not_awaited()
    assert events == ["session_open", "session_closed"]


async def test_shared_provider_call_runs_after_session_closed_and_write_is_fenced(monkeypatch):
    from unittest.mock import AsyncMock

    source, events = _shared_source(), []
    frozen = dict(source)
    vector = [0.125, 0.5]
    monkeypatch.setattr(diary_recall_jobs, "get_sessionmaker", lambda: lambda: _SessionScope(events))
    load = AsyncMock(return_value=source)
    monkeypatch.setattr(diary_recall_jobs.shared, "load_shared", load)

    async def embed(value):
        assert events == ["session_open", "session_closed"]
        assert value == frozen["search_text"]
        events.append("provider")
        # The callback must keep the loaded snapshot, not a mutable result view.
        source["source_hash"] = "changed-after-load"
        return vector

    monkeypatch.setattr(diary_recall_repo, "embed_text", embed)
    write = AsyncMock(return_value=False)  # Another worker already filled it.
    monkeypatch.setattr(diary_recall_jobs.shared, "write_shared", write)
    job = _job({"shared_id": str(frozen["id"])}, user_id=None)
    result = await diary_recall_jobs.handle_diary_template_embed(job)
    assert result.result_code == "ok"
    assert events == ["session_open", "session_closed", "provider"]
    write.assert_not_awaited()
    apply_session = object()
    await result.apply_domain(apply_session)
    write.assert_awaited_once_with(apply_session, source=frozen, vector=vector)


async def test_shared_provider_outage_is_retryable_without_a_write(monkeypatch):
    from unittest.mock import AsyncMock

    source, events = _shared_source(), []
    monkeypatch.setattr(diary_recall_jobs, "get_sessionmaker", lambda: lambda: _SessionScope(events))
    monkeypatch.setattr(diary_recall_jobs.shared, "load_shared", AsyncMock(return_value=source))

    async def embed(value):
        assert events == ["session_open", "session_closed"]
        raise TimeoutError("provider unavailable")

    monkeypatch.setattr(diary_recall_repo, "embed_text", embed)
    write = AsyncMock()
    monkeypatch.setattr(diary_recall_jobs.shared, "write_shared", write)
    with pytest.raises(consumer.JobRetry, match="embed_failed"):
        await diary_recall_jobs.handle_diary_template_embed(
            _job({"shared_id": str(source["id"])}, user_id=None),
        )
    write.assert_not_awaited()


async def test_old_owned_job_routes_to_shared_only_in_apply_domain(monkeypatch):
    from unittest.mock import AsyncMock

    events = []
    uid, did = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(diary_recall_jobs, "get_sessionmaker", lambda: lambda: _SessionScope(events))
    route = diary_recall_jobs.shared.SharedDiaryRoute(user_id=uid, diary_id=did)
    monkeypatch.setattr(diary_recall_repo, "embed_diary_document", AsyncMock(return_value=route))
    ensure, embed, write = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(diary_recall_jobs.shared, "ensure_for_diary", ensure)
    monkeypatch.setattr(diary_recall_repo, "embed_text", embed)
    monkeypatch.setattr(diary_recall_repo, "write_diary_embedding", write)
    result = await diary_recall_jobs.handle_diary_recall_embed(_job({"diary_id": str(did)}, user_id=uid))
    assert result.result_code == "shared"
    assert events == ["session_open", "session_closed"]
    ensure.assert_not_awaited()
    embed.assert_not_awaited()
    write.assert_not_awaited()
    session = object()
    await result.apply_domain(session)
    ensure.assert_awaited_once_with(session, user_id=uid, diary_id=did)

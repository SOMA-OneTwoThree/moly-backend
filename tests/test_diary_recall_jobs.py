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

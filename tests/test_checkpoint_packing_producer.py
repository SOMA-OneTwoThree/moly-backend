"""New roots adopt packing; existing execution snapshots keep their protocol."""
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.services import checkpoint as cp, checkpoint_repo as repo, context_retention, jobs


def plan(version=cp.RETENTION_SUMMARIZER_VERSION):
    return cp.CheckpointPlan(4, (1, 2, 3, 4), "a" * 64, None, None,
                             version=version, privacy_epoch=0)


@pytest.mark.parametrize("version", [cp.RETENTION_SUMMARIZER_VERSION, cp.SUMMARIZER_VERSION])
async def test_only_new_retention_root_gets_strategy_without_changing_shared_plan(monkeypatch, version):
    value = plan(version)
    before = value.payload()
    enqueue = AsyncMock(return_value=uuid.uuid4())
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "reserve_checkpoint_dependencies", AsyncMock())
    await repo.enqueue_checkpoint(None, user_id=uuid.uuid4(), plan=value)
    assert value.payload() == before and "packing_version" not in before
    actual = enqueue.await_args.kwargs["payload"]
    assert actual == ({**before, "packing_version": cp.PACKING_VERSION}
                      if version == cp.RETENTION_SUMMARIZER_VERSION else before)


@pytest.mark.parametrize("metadata,merged", [
    ({}, True), ({"packing_version": "packed-v1"}, True),
    ({"packing_version": "unknown"}, False),
    ({"packing_version": "packed-v1", "model_attempts_remaining": 2}, False),
    ({"packing_version": "packed-v1", "packing_chunk_cap": 1}, False),
    ({"progress_state": {}}, False), ({"progress_page_proof": {}}, False),
    ({"mode": "publish_retention", "completed_state": {}}, False),
    ({"mode": "reverify"}, False),
])
async def test_ready_merge_preserves_strategy_and_never_rebinds_execution_metadata(monkeypatch, metadata, merged):
    uid, identity = uuid.uuid4(), uuid.uuid4()
    old = {**plan().payload(), "through_message_id": 2, **metadata}
    writes = []

    class Result:
        def __init__(self, row=None):
            self.row = row
        def first(self):
            return self.row

    class Session:
        async def execute(self, sql, params):
            if sql is repo._RETENTION_PENDING:
                return Result((identity, "ready", 0, datetime.now(timezone.utc), old, None))
            if sql is repo._SAVE_RETENTION_TARGET:
                return Result()
            assert sql is repo._MERGE_RETENTION_READY
            writes.append(params)
            return Result((identity,))

    monkeypatch.setattr(repo, "advisory_xact_lock", AsyncMock())
    monkeypatch.setattr(repo, "load_latest", AsyncMock(return_value=None))
    monkeypatch.setattr(repo, "read_memory_generation", AsyncMock(return_value=0))
    monkeypatch.setattr(cp, "plan", lambda *args, **kwargs: plan())
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(repo, "reserve_checkpoint_dependencies", AsyncMock())
    sources = [cp.SourceMessage(i, "user", "normal", f"synthetic {i}") for i in range(1, 5)]
    result = await repo._maybe_enqueue_retention(Session(), user_id=uid, messages=sources,
        keep_from_message_id=5, reset_triggered=True)
    assert result == (identity if merged else None)
    assert len(writes) == int(merged)
    if merged:
        actual = json.loads(writes[0]["payload"])
        assert actual["through_message_id"] == 4
        assert actual.get("packing_version") == old.get("packing_version")
        assert ("packing_version" in actual) == ("packing_version" in old)
    assert old == {**plan().payload(), "through_message_id": 2, **metadata}

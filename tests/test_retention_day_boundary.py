"""하루 경계에서 구형 계약 추출로 돌아가지 않는지 확인한다."""
from datetime import date
import uuid

import pytest

from app.services import config_store, context_retention, jobs, memory_pipeline


@pytest.mark.parametrize("unused,checkpoint,enabled,paused,expected", [
    (True, False, True, False, "retention"),
    (True, False, False, False, "legacy"),
    (False, True, False, False, None),
    (False, True, True, False, "retention"),
    (False, False, False, False, "legacy"),
    (True, True, True, True, None),
])
async def test_day_boundary_preserves_retention_contract_path(
    monkeypatch, unused, checkpoint, enabled, paused, expected,
):
    uid, made = uuid.uuid4(), []
    class Result:
        def __init__(self, row):
            self.row = row
        def first(self):
            return self.row
    class Session:
        async def scalar(self, statement, params):
            return checkpoint
        async def execute(self, statement, params):
            if statement is memory_pipeline._DAY_BOUNDARY:
                return Result((date(2026, 11, 2), date(2026, 11, 1)))
            return Result((4, checkpoint, 22))
    async def config(*args):
        return {"context_retention_v2_enabled": enabled}
    async def flag(session, *, lock=False):
        assert lock
        return enabled
    async def adopted(*args, **kwargs):
        return enabled or checkpoint
    async def enqueue(*args, **kwargs):
        made.append(kwargs)
    async def retained(*args, **kwargs):
        assert kwargs == {"user_id": uid, "generation": 4, "through_id": 22}
        made.append({"job_type": "retention"})
    monkeypatch.setattr(config_store, "get_config_values", config)
    monkeypatch.setattr(context_retention, "enabled_now", flag)
    monkeypatch.setattr(context_retention, "adopted_user", adopted)
    monkeypatch.setattr(context_retention, "enqueue_contract_processing", retained)
    monkeypatch.setattr(jobs, "enqueue", enqueue)
    assert await memory_pipeline.enqueue_day_boundary_jobs(Session(), uid, turn_seq=9,
        contract_paused=paused) == "2026-11-01"
    assert [item["job_type"] for item in made[:2]] == ["mem0_reconsolidate", "relationship_project"]
    contracts = [item for item in made if item["job_type"] in {"retention", "contract_compile"}]
    assert len(contracts) == (expected is not None)
    if expected == "legacy":
        assert contracts[0]["payload"] == {"after_message_id": 0, "privacy_epoch": 0}
    elif expected == "retention":
        assert contracts[0]["job_type"] == "retention"

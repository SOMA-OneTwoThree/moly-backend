"""Independent suffix dependency/resume review; synthetic sessions only."""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.core.errors import AppError
from app.services import chat, context_compat, context_retention, contract_repo, jobs
from tests.test_chat_pressure_handoff import handoff as handoff, execute
from tests.test_guard_context_v2_integration_review import setup as setup


UID = uuid.UUID(int=700)


def baseline():
    return context_compat.Baseline(str(uuid.UUID(int=701)), str(UID), 0, 0, 100, 101,
                                 None, 0, None, None, None, 80, None, None)


class Result:
    def __init__(self, row=None):
        self.row = row
    def first(self):
        return self.row
    def all(self):
        return [] if self.row is None else [self.row]


@pytest.mark.parametrize("change,expected", [(None, True), ("binding", False), ("epoch", False), ("dead", False)])
async def test_contract_resume_keeps_original_binding_epoch_partial_and_budget(monkeypatch, change, expected):
    value = baseline()
    original = {"retention_enabled": True, "memory_generation": 0,
        "through_message_id": 200, "after_message_id": 100, "privacy_epoch": 0,
        "baseline_binding": value.payload(), "partial_message_id": 150, "partial_offset": 12,
        "partial_source_hash": "a"*64, "adaptive_output_fallback": True,
        "adaptive_output_attempts_remaining": 1}
    if change == "binding":
        original["baseline_binding"] = replace(value, baseline_id=str(uuid.UUID(int=702))).payload()
    if change == "epoch":
        original["privacy_epoch"] = 1
    before = dict(original)
    class Session:
        async def get(self, *args):
            return SimpleNamespace(memory_generation=0, retention_compat_json=value.payload())
        async def scalar(self, statement, params):
            assert statement is context_retention._CONTRACT_ACTIVE
            return None
        async def execute(self, statement, params):
            if statement is context_retention._CONTRACT_LATEST_TERMINAL:
                # 방금 dead가 된 원본: 다시 만들 간격(30분)이 아직 지나지 않았다.
                return Result(("dead" if change == "dead" else "succeeded", uuid.UUID(int=706),
                               datetime.now(timezone.utc), "contract_retention_evidence", None))
            if statement is context_retention._CONTRACT_BASE_JOB:
                return Result((uuid.UUID(int=703),))
            assert "SELECT j.id,j.payload FROM async_jobs" in str(statement)
            return Result((uuid.UUID(int=704), original))
    monkeypatch.setattr(context_retention, "privacy_epoch_matches", AsyncMock(return_value=True))
    monkeypatch.setattr(contract_repo, "effective_contract_cursor", AsyncMock(return_value=101))
    queued = AsyncMock(return_value=uuid.UUID(int=705))
    monkeypatch.setattr(jobs, "enqueue", queued)
    made = await context_retention.enqueue_contract_processing(Session(), user_id=UID, generation=0,
                                                             through_id=200, privacy_epoch=0)
    assert (made is not None) is expected
    assert original == before
    if expected:
        payload = queued.await_args.kwargs["payload"]
        assert payload == {**before, "after_message_id": 101}
        assert payload["adaptive_output_attempts_remaining"] == 1
        assert queued.await_args.kwargs["queue"] == jobs.QUEUE_MEMORY
    else:
        queued.assert_not_awaited()


@pytest.mark.parametrize("suffix_correction", [False, True])
async def test_correction_scan_starts_at_baseline_and_keeps_new_correction(monkeypatch, suffix_correction):
    value = baseline()
    history = [(50, "Actually, I meant something else."),
               (101, "Actually, I meant something else." if suffix_correction else "ordinary new topic")]
    cursors = []
    class Session:
        async def get(self, *args):
            return SimpleNamespace(memory_generation=0, retention_compat_json=value.payload())
        async def execute(self, statement, params):
            assert "m.id>:after_id" in str(statement)
            assert "m.id>:contract_through" in str(statement)
            cursors.append(params["after_id"])
            return SimpleNamespace(all=lambda: [entry for entry in history if entry[0] > params["after_id"]])
    monkeypatch.setattr(contract_repo, "effective_contract_cursor", AsyncMock(return_value=100))
    assert await context_retention.pending_correction(Session(), user_id=UID, generation=0,
                                                      current_text="ordinary current topic") is suffix_correction
    assert cursors == [100]


async def test_new_suffix_backlog_still_fails_instead_of_silently_ignoring_it(monkeypatch):
    value = baseline()
    class Session:
        async def get(self, *args):
            return SimpleNamespace(memory_generation=0, retention_compat_json=value.payload())
        async def execute(self, statement, params):
            assert params["after_id"] == 100
            return Result((101, "x"*20_000))
    monkeypatch.setattr(contract_repo, "effective_contract_cursor", AsyncMock(return_value=100))
    with pytest.raises(context_retention.RetentionError, match="correction_processing_backlog"):
        await context_retention.pending_correction(Session(), user_id=UID, generation=0, current_text="hello")


async def test_pressure_resume_must_not_rebind_the_original_baseline(handoff, monkeypatch):
    *_, wait = handoff
    original = chat.context_compat.load.return_value
    changed = replace(original, baseline_id=str(uuid.UUID(int=799)))

    async def completed(*args):
        chat.context_compat.load.return_value = changed
        return True

    wait.side_effect = completed
    with pytest.raises(AppError) as caught:
        await execute()
    assert caught.value.code == "CHAT_TURN_STALE"
    chat.llm.generate.assert_not_awaited()
    chat._accumulate_tokens.assert_not_awaited()

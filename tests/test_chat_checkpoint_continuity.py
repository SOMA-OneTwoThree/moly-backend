"""Synthetic reproduction of the reset-before-summary coverage gap."""
import pytest

from app.config import settings
from app.services import chat
from app.services.chat_turns import verify_publish as verify_publish_actual
from tests.test_chat import FakeSession
from tests.test_chat_caching import UID, _msg


async def test_pending_first_checkpoint_preserves_open_thread(monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)
    rows = [
        _msg(i, "user" if i % 2 else "moly",
             "SYNTHETIC_OPEN_THREAD" if i == 1 else f"Synthetic turn {i}")
        for i in range(39, 0, -1)
    ]
    window = await chat._context(
        FakeSession(execute_items=rows), UID, 0,
        current_text="Can we continue that story?",
    )
    convo, _new_anchor, _lead = window.conversation, window.new_anchor, window.lead
    # No summary exists yet. The current implementation has no coverage input and
    # unconditionally removes this source at the 40-message threshold.
    assert any("SYNTHETIC_OPEN_THREAD" in item["content"] for item in convo)


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)


def segment(n, *, start=1, size=None):
    return [
        _msg(i, "user" if i % 2 else "moly", "x" * size if size else f"Synthetic turn {i}")
        for i in range(start + n - 1, start - 1, -1)
    ]


async def test_pending_window_and_request_boundary_are_independent(enabled):
    window = await chat._context(FakeSession(execute_items=segment(40)), UID, 0)
    assert len(window.conversation) == 40
    assert window.new_anchor is None
    assert window.checkpoint_keep_from == 21
    assert not window.incomplete


async def test_published_coverage_advances_only_to_covered_boundary(enabled):
    window = await chat._context(
        FakeSession(execute_items=segment(44)), UID, 0, checkpoint_through=20,
    )
    assert window.new_anchor == 21
    assert len(window.conversation) == 24
    assert window.checkpoint_keep_from is None
    assert "turn 21" in window.conversation[0]["content"]


async def test_late_smaller_coverage_never_moves_anchor_back(enabled):
    window = await chat._context(
        FakeSession(execute_items=segment(10, start=61)), UID, 61, checkpoint_through=20,
    )
    assert window.new_anchor is None
    assert window.incomplete
    assert len(window.conversation) == 10


async def test_pending_history_is_append_only(enabled):
    first = await chat._context(FakeSession(execute_items=segment(40)), UID, 0)
    second = await chat._context(FakeSession(execute_items=segment(42)), UID, 0)
    assert second.conversation[:len(first.conversation)] == first.conversation


async def test_message_pressure_is_bounded_and_marked(enabled):
    session = FakeSession(execute_items=segment(121))
    window = await chat._context(session, UID, 0)
    assert window.incomplete
    assert len(window.conversation) <= settings.context_keep_messages
    assert window.new_anchor == window.checkpoint_keep_from
    assert window.new_anchor > 1


async def test_character_pressure_is_bounded_and_marked(enabled):
    window = await chat._context(FakeSession(execute_items=segment(35, size=2000)), UID, 0)
    assert window.incomplete
    assert sum(len(item["content"]) for item in window.conversation) < settings.context_hard_chars
    assert window.new_anchor is not None


async def test_single_oversized_stored_message_cannot_defeat_cap(enabled):
    window = await chat._context(
        FakeSession(execute_items=segment(1, size=100_000)), UID, 0, current_text="Hi",
    )
    assert window.incomplete
    assert window.new_anchor == 2
    assert len(window.conversation) == 1
    assert window.conversation[0]["content"].endswith("Hi")


async def test_coverage_can_resolve_sentinel_without_pressure_fallback(enabled):
    window = await chat._context(
        FakeSession(execute_items=segment(121, start=10)), UID, 0, checkpoint_through=110,
    )
    assert window.new_anchor == 111
    assert not window.incomplete
    assert len(window.conversation) == 20


async def test_checkpoint_disabled_keeps_existing_reset_policy(monkeypatch):
    monkeypatch.setattr(settings, "context_checkpoint_enabled", False)
    window = await chat._context(FakeSession(execute_items=segment(40)), UID, 0)
    assert window.new_anchor == 21
    assert window.checkpoint_keep_from is None
    assert not window.incomplete


async def test_query_fetches_sentinel_and_never_reads_behind_anchor(enabled):
    class Session(FakeSession):
        async def execute(self, stmt, params=None):
            compiled = stmt.compile().params
            assert 61 in compiled.values()
            assert settings.context_hard_msg_cap + 1 in compiled.values()
            return await super().execute(stmt, params)
    await chat._context(Session(execute_items=segment(10, start=61)), UID, 61, checkpoint_through=20)


async def test_pending_job_blocks_duplicate_request_only_while_active(monkeypatch):
    from app.services import checkpoint_repo
    calls = []
    class Result:
        def first(self):
            return (1,)
    class Session:
        async def execute(self, stmt, params):
            calls.append(str(stmt))
            return Result()
    monkeypatch.setattr(settings, "context_checkpoint_enabled", True)
    result = await checkpoint_repo.maybe_enqueue(
        Session(), user_id=UID, messages=[object()], keep_from_message_id=21, reset_triggered=True,
    )
    assert result is None
    assert len(calls) == 1
    assert "('ready', 'running')" in calls[0]
    assert "memory_generation" in calls[0]
    assert "j.user_id = :user_id" in calls[0]


async def test_publish_verification_checks_and_locks_memory_generation():
    import uuid
    from app.services import chat_turns
    from app.core.errors import AppError
    lease = chat_turns.Lease(uuid.uuid4(), 4, 8)
    class Result:
        def first(self):
            return None
    class Session:
        async def execute(self, statement, params):
            assert params["memory_generation"] == 3
            assert "c.memory_generation=:memory_generation" in str(statement)
            assert "FOR UPDATE OF a,c" in str(statement)
            return Result()
    with pytest.raises(AppError) as error:
        await verify_publish_actual(Session(), user_id=uuid.UUID(UID), lease=lease, memory_generation=3)
    assert error.value.code == "CHAT_TURN_STALE"

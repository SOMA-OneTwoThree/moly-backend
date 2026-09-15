"""Only today's selected mood is transient context; temporal history stays stable."""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

from app.services import chat, checkpoint, mood_context
from tests.test_chat import FakeSession
from tests.test_chat_turn_context_wiring import _post

NOW = datetime(2026, 9, 14, 15, 1, tzinfo=timezone.utc)  # 09/15 00:01 KST
TODAY = date(2026, 9, 15)
UID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _message(i, at, content="배불러", sender="user"):
    return SimpleNamespace(
        id=i, sender=sender, content=content, created_at=at, activity_date=date(2026, 9, 14),
    )


async def test_midnight_history_stays_fixed_when_time_and_mood_change(monkeypatch):
    history = [_message(2, NOW - timedelta(minutes=2), "그랬구나", "moly"),
               _message(1, NOW - timedelta(minutes=2))]
    earlier, _, _ = await chat._context(
        FakeSession(execute_items=history), UID, 0, current_text="많이 먹었나 봐",
        current_date=date(2026, 9, 14), current_at=NOW,
    )
    later, _, _ = await chat._context(
        FakeSession(execute_items=history), UID, 0, current_text="저녁 뭐 먹지?",
        current_date=TODAY, current_at=NOW + timedelta(hours=18),
    )
    assert earlier[:-1] == later[:-1]  # Prefix never gets relative age annotations.
    assert earlier[0]["content"].startswith("[2026-09-14T23:59+09:00]")
    assert earlier[1]["content"] == "그랬구나"  # Same-minute duplicate is omitted.
    assert earlier[-1]["content"].startswith("[2026-09-15T00:01+09:00]")
    assert later[-1]["content"].startswith("[2026-09-15T18:01+09:00]")


def test_missing_timestamp_is_not_replaced_by_activity_date():
    convo = [{"role": "user", "content": "배불렀어"}]
    chat._mark_dates(convo, [_message(1, None)])
    assert convo[0]["content"] == "[time unknown]\n배불렀어"


@pytest.mark.parametrize("zone,expected", [
    ("Asia/Seoul", "2026-09-15T00:01+09:00"),
    ("America/New_York", "2026-09-14T11:01-04:00"),
    ("broken/zone", "2026-09-15T00:01+09:00"),
])
def test_timestamps_use_calendar_time_and_explicit_offset(zone, expected):
    assert chat._time_label(NOW, zone) == f"[{expected}]"


def test_elapsed_time_uses_real_minutes_across_midnight_and_dst():
    assert "Minutes since previous turn: 2." in chat._time_context(
        NOW, NOW - timedelta(minutes=2), "Asia/Seoul",
    )
    # Fall-back repeats 01:30 in New York, but the instants are an hour apart.
    first = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    block = chat._time_context(first + timedelta(hours=1), first, "America/New_York")
    assert "01:30-05:00" in block and "01:30-04:00" in block
    assert "Minutes since previous turn: 60." in block
    unknown = chat._time_context(NOW, None, "Asia/Seoul")
    assert "last_turn=" not in unknown and "Minutes since" not in unknown


async def test_mood_changes_only_the_volatile_system_slot(monkeypatch):
    block = AsyncMock(side_effect=[
        "[User mood selection] 2026-09-15: 신남",
        "[User mood selection] 2026-09-15: unknown",
    ])
    monkeypatch.setattr(mood_context, "today_block", block)
    before, after = {}, {}
    first_session = FakeSession()
    await _post(first_session, monkeypatch, capture=before)
    await _post(FakeSession(), monkeypatch, capture=after)
    assert before["system"] == after["system"]
    assert "신남" not in "\n".join(before["system"])
    assert before["convo"][-2]["role"] == "system"
    assert "신남" in before["convo"][-2]["content"]
    assert "신남" not in after["convo"][-2]["content"]
    assert "신남" not in before["convo"][-1]["content"]
    assert all("신남" not in (getattr(row, "content", "") or "")
               for row in first_session.added)
    assert block.await_count == 2


def test_checkpoint_time_affects_v4_hash_but_not_pending_v3_jobs():
    a = checkpoint.SourceMessage(1, "user", "normal", "배불러", NOW)
    b = checkpoint.SourceMessage(1, "user", "normal", "배불러", NOW + timedelta(hours=6))
    assert checkpoint.source_hash(previous=None, messages=[a]) != checkpoint.source_hash(
        previous=None, messages=[b],
    )
    legacy = checkpoint.LEGACY_SUMMARIZER_VERSION
    assert checkpoint.source_hash(previous=None, messages=[a], version=legacy) == (
        checkpoint.source_hash(previous=None, messages=[b], version=legacy)
    )
    assert "2026-09-14T15:01" in checkpoint.render_conversation([a])
    assert "2026-09-14" not in checkpoint.render_conversation([a], version=legacy)


async def test_today_lookup_failure_does_not_claim_no_record():
    # Missing begin_nested simulates failure before querying; the prompt reports unavailability.
    block = await mood_context.today_block(None, UID, TODAY)
    assert block == "[User mood selection] 2026-09-15: unknown"


async def test_today_query_never_selects_notes_or_record_timestamps():
    @asynccontextmanager
    async def savepoint():
        yield

    session = SimpleNamespace(
        begin_nested=savepoint,
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: "neutral")),
    )
    assert await mood_context.today_block(session, UID, TODAY) == "[User mood selection] 2026-09-15: 평범"
    stmt = session.execute.call_args.args[0].compile(dialect=postgresql.dialect())
    sql = str(stmt)
    assert all(name not in sql for name in ("note", "created_at", "updated_at"))
    assert "mood_entries.user_id =" in sql and "mood_entries.entry_date =" in sql
    assert set(next(value for value in stmt.params.values() if isinstance(value, list))) == {
        "annoyed", "tired", "neutral", "content", "excited",
    }
    assert UID in stmt.params.values() and TODAY in stmt.params.values()
